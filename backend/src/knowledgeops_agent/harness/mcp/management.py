"""MCP候选管理：导入不启用，显式准备走队列，测试成功后再提交活动版本。

扩展操作同时是候选与持久投递事实。安装重试由用户发起；重复消息不能
重新安装，崩溃租约只能转为interrupted。更新候选失败不改变活动连接。
"""

from __future__ import annotations

import asyncio
import builtins
import json
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.harness.mcp.imports import parse_import
from knowledgeops_agent.harness.mcp.installations import (
    MCPInstallStorage,
    MCPPreparer,
    PreparationError,
)
from knowledgeops_agent.harness.mcp.registry import (
    MCPRegistry,
    configuration,
    connection_view,
    descriptor,
)
from knowledgeops_agent.harness.mcp.secrets import MCPSecretStore, config_references
from knowledgeops_agent.harness.mcp.transport import MCPConnectionConfig, connected_tools
from knowledgeops_agent.harness.runtime.resources import content_hash
from knowledgeops_agent.persistence.models import (
    ExtensionOperation,
    MCPConnection,
    MCPInstallation,
    utcnow,
)

KIND = 'mcp_candidate'


def operation_view(row: ExtensionOperation) -> dict:
    return {key: getattr(row, key) for key in ('id', 'target', 'status', 'expected_revision',
            'parameters', 'result', 'error_code', 'attempts', 'cancel_requested')}


class MCPManagement:
    def __init__(self, sessions, *, root: Path | None = None, preparer=None, live_check=None):
        self.sessions = sessions
        self.storage = MCPInstallStorage(root)
        self.secrets = MCPSecretStore(self.storage.root)
        self.preparer = preparer or MCPPreparer(self.storage)
        self.live_check = live_check

    @staticmethod
    def preview(value: dict) -> list[dict]:
        imported = parse_import(value)
        for item in imported:
            MCPInstallStorage().reject_external_references(item.config)
        return [item.safe_view() for item in imported]

    def _operation(self, session, identity: str, *, lock=False) -> ExtensionOperation:
        query = select(ExtensionOperation).where(ExtensionOperation.id == identity,
                                                ExtensionOperation.kind == KIND)
        row = session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise KeyError('MCP候选不存在')
        return row

    def get(self, identity: str) -> dict:
        with self.sessions() as session:
            return operation_view(self._operation(session, identity))

    def list(self) -> builtins.list[dict]:
        with self.sessions() as session:
            return [operation_view(row) for row in session.scalars(select(ExtensionOperation)
                .where(ExtensionOperation.kind == KIND, ExtensionOperation.status.notin_(['active', 'removed', 'archived']))
                .order_by(ExtensionOperation.created_at.desc()))]

    def removal_preview(self, identity: str):
        from sqlalchemy import func

        from knowledgeops_agent.harness.mcp.lifecycle import PENDING
        from knowledgeops_agent.persistence.models import MCPCall

        row = MCPRegistry(self.sessions).get(identity)
        with self.sessions() as session:
            count = session.scalar(select(func.count()).select_from(MCPInstallation).where(
                MCPInstallation.connection_id == identity, MCPInstallation.status != 'removed'))
            pending = session.scalar(select(func.count()).select_from(MCPCall).where(
                MCPCall.connection_id == identity, MCPCall.status.in_(PENDING)))
        return {'revision': row['revision'], 'name': row['name'], 'owned_installations': count,
                'unresolved_calls': pending, 'has_managed_data': self.storage.checked('data', identity).exists()}

    def maintain(self, *, expire=True, retention_days=30):
        from knowledgeops_agent.harness.mcp.lifecycle import MCPCleaner

        return MCPCleaner(self).maintain(expire=expire, retention_days=retention_days)

    @staticmethod
    def _current(session, target, revision):
        current = session.get(MCPConnection, target)
        if revision is None:
            if current is not None:
                raise ValueError('MCP目标身份已被占用')
        elif not current or current.deleted or current.revision != revision:
            raise ValueError('MCP活动配置修订已变化，请刷新后重新导入候选')
        return current

    def import_config(self, value: dict, *, connection_id: str | None = None,
                      expected_revision: int | None = None) -> builtins.list[dict]:
        imported = parse_import(value)
        for item in imported:
            self.storage.reject_external_references(item.config)
        if (connection_id is None) != (expected_revision is None) or (connection_id and len(imported) != 1):
            raise ValueError('更新必须指定一个连接及其修订号')
        identities = []
        with self.sessions.begin() as session:
            lock_control(session)
            if connection_id:
                self._current(session, connection_id, expected_revision)
            for item in imported:
                row = ExtensionOperation(kind=KIND, target=connection_id or str(uuid4()), status='importing',
                    expected_revision=expected_revision,
                    parameters={'name': item.name, 'config': item.config, 'installation': item.installation,
                                'owned_secret_refs': sorted(item.secret_values)},
                    result={} if item.installation else {'config': item.config},
                    expires_at=utcnow() + timedelta(days=7))
                session.add(row)
                session.flush()
                from knowledgeops_agent.harness.mcp.lifecycle import own_secret
                for reference in item.secret_values:
                    own_secret(session, reference)
                identities.append(row.id)
        # 先提交身份再写秘密；中断不会产生没有可见操作身份的密钥。原值不进入PG。
        for identity, item in zip(identities, imported, strict=True):
            try:
                if item.secret_values:
                    self.secrets.update(item.secret_values)
            except (ValueError, OSError):
                with self.sessions.begin() as session:
                    row = self._operation(session, identity, lock=True)
                    row.status, row.error_code = 'failed', 'secret_write_failed'
                continue
            with self.sessions.begin() as session:
                row = self._operation(session, identity, lock=True)
                row.status = 'imported' if item.installation else 'prepared'
                row.result = {} if item.installation else {'config': item.config}
                row.updated_at = utcnow()
        return [self.get(identity) for identity in identities]

    def prepare(self, identity: str) -> dict:
        """可信UI的明确操作才创建准备身份；HTTP本身不等待包管理器。"""
        with self.sessions.begin() as session:
            lock_control(session)
            row = self._operation(session, identity, lock=True)
            if not row.parameters.get('installation'):
                raise ValueError('此连接无需安装依赖，可直接测试')
            if row.status not in {'imported', 'failed', 'interrupted', 'cancelled'}:
                raise ValueError('此候选正在处理或已经准备，请刷新状态')
            self._current(session, row.target, row.expected_revision)
            if session.scalar(select(ExtensionOperation.id).where(ExtensionOperation.kind == 'mcp_cleanup',
                ExtensionOperation.target == row.target, ExtensionOperation.status == 'running',
                ExtensionOperation.lease_until > utcnow()).limit(1)):
                raise ValueError('上一批文件正在回收，请稍后再准备依赖')
            installation = MCPInstallation(connection_id=row.target, operation_id=row.id,
                                           request=row.parameters['installation'])
            session.add(installation)
            session.flush()
            row.status, row.error_code, row.cancel_requested = 'queued', None, False
            row.attempts += 1
            row.result = {'installation_id': installation.id, 'progress': []}
            row.lease_owner, row.lease_until = None, None
            row.updated_at = utcnow()
            return operation_view(row)

    def execute(self, identity: str, generation: int) -> dict | None:
        token = str(uuid4())
        with self.sessions.begin() as session:
            row = self._operation(session, identity, lock=True)
            if row.status != 'queued' or row.attempts != generation or row.cancel_requested:
                return None
            row.status, row.lease_owner = 'preparing', token
            row.lease_until = utcnow() + timedelta(seconds=30)
            target, installation, request = row.target, row.result['installation_id'], row.parameters['installation']
            base_config = row.parameters['config']
            restore_from = row.parameters.get('restore_from')
        last_check = 0.0

        def check(force=False):
            nonlocal last_check
            now = time.monotonic()
            if not force and now - last_check < 0.5:
                return
            if self.live_check is not None:
                self.live_check()
            with self.sessions.begin() as session:
                current = self._operation(session, identity, lock=True)
                if current.lease_owner != token or current.status != 'preparing':
                    raise PreparationError('lease_lost')
                if current.cancel_requested:
                    raise PreparationError('cancelled')
                current.lease_until = utcnow() + timedelta(seconds=30)
                current.updated_at = utcnow()
            last_check = now

        def report(phase, status):
            with self.sessions.begin() as session:
                current = self._operation(session, identity, lock=True)
                if current.lease_owner != token:
                    raise PreparationError('lease_lost')
                progress = [*current.result.get('progress', []), {'phase': phase, 'status': status,
                            'at': utcnow().isoformat()}][-32:]
                current.result = {**current.result, 'progress': progress}
                current.updated_at = utcnow()

        try:
            restored = {}
            if restore_from:
                with self.storage.lock(restore_from):
                    manifest, locks = self.storage.portable(restore_from)
                if manifest['request'] != request or manifest['connection_id'] != target:
                    raise PreparationError('restore_lock_mismatch')
                restored['lock_files'] = locks
            result = self.preparer.prepare(target, installation, identity, request, check=check, report=report, **restored)
            check(force=True)
            config = configuration({**base_config, 'command': result['command'], 'installation_id': installation,
                                    'cwd': base_config.get('cwd') or str(self.storage.service_directory('data', target))})
            with self.sessions.begin() as session:
                row = self._operation(session, identity, lock=True)
                if row.lease_owner != token or row.cancel_requested or row.status != 'preparing':
                    raise PreparationError('cancelled' if row.cancel_requested else 'lease_lost')
                record = session.get(MCPInstallation, installation)
                record.status, record.manifest, record.updated_at = 'prepared', result, utcnow()
                row.result = {**row.result, 'config': config}
                row.status, row.error_code = 'prepared', None
                row.lease_owner, row.lease_until, row.updated_at = None, None, utcnow()
        except Exception as error:
            with self.sessions.begin() as session:
                row = self._operation(session, identity, lock=True)
                if row.lease_owner == token:
                    row.status = 'cancelled' if row.cancel_requested else 'failed'
                    row.error_code = str(error) if isinstance(error, PreparationError) else 'preparation_failed'
                    row.lease_owner, row.lease_until, row.updated_at = None, None, utcnow()
                    record = session.get(MCPInstallation, installation)
                    record.status, record.updated_at = 'failed', utcnow()
        return self.get(identity)

    def cancel(self, identity: str) -> dict:
        with self.sessions.begin() as session:
            lock_control(session)
            row = self._operation(session, identity, lock=True)
            if row.status in {'active', 'removed'}:
                raise ValueError('已激活候选不能取消；请管理活动连接')
            row.cancel_requested, row.updated_at = True, utcnow()
            if row.status not in {'preparing', 'testing'}:
                row.status = 'cancelled'
            from knowledgeops_agent.harness.mcp.lifecycle import schedule
            schedule(session, row.target, name=row.parameters.get('name', ''))
            return operation_view(row)

    def discard(self, identity: str) -> dict:
        """移出日常候选列表，保留安装归属供后续安全回收，不改变活动连接。"""
        with self.sessions.begin() as session:
            lock_control(session)
            row = self._operation(session, identity, lock=True)
            if row.status in {'active', 'preparing', 'testing', 'importing'}:
                raise ValueError('请先取消正在执行的操作，活动连接请从连接卡片管理')
            row.status, row.cancel_requested, row.updated_at = 'removed', True, utcnow()
            row.expires_at = utcnow()
            from knowledgeops_agent.harness.mcp.lifecycle import schedule
            schedule(session, row.target, name=row.parameters.get('name', ''))
            return operation_view(row)

    def recover_expired(self, *, limit=20) -> int:
        """只将失去执行者的操作变成可见中断，不能隐式再下载或再运行安装脚本。"""
        with self.sessions.begin() as session:
            rows = list(session.scalars(select(ExtensionOperation).where(ExtensionOperation.kind == KIND,
                ((ExtensionOperation.status.in_(['preparing', 'testing'])) & (ExtensionOperation.lease_until < utcnow()))
                | ((ExtensionOperation.status == 'importing') & (ExtensionOperation.created_at < utcnow() - timedelta(minutes=2))))
                .limit(limit).with_for_update(skip_locked=True)))
            for row in rows:
                row.status, row.error_code = 'interrupted', 'worker_interrupted'
                row.lease_owner, row.lease_until, row.updated_at = None, None, utcnow()
                record = session.get(MCPInstallation, row.result.get('installation_id')) if row.result.get('installation_id') else None
                if record and record.status == 'preparing':
                    record.status, record.updated_at = 'failed', utcnow()
            return len(rows)

    def test(self, identity: str) -> dict:
        token = str(uuid4())
        with self.sessions.begin() as session:
            lock_control(session)
            row = self._operation(session, identity, lock=True)
            if row.status not in {'prepared', 'tested', 'failed', 'interrupted'} or not row.result.get('config'):
                raise ValueError('请先完成依赖准备，再测试候选')
            config = row.result['config']
            if config.get('installation_id'):
                installation = session.get(MCPInstallation, config['installation_id'])
                if not installation or installation.status != 'prepared':
                    raise ValueError('候选安装已进入回收，请重新准备')
            row.status, row.lease_owner, row.cancel_requested = 'testing', token, False
            row.lease_until = utcnow() + timedelta(seconds=config['timeout_seconds'] + 30)
        try:
            secrets = self.secrets.resolve(config_references(config))

            async def discover():
                async with connected_tools(MCPConnectionConfig.model_validate(config), secrets=secrets) as tools:
                    return [descriptor(tool) for tool in tools]

            catalog = asyncio.run(asyncio.wait_for(discover(), timeout=config['timeout_seconds']))
            if len(catalog) > 200 or len(json.dumps(catalog)) > 2_000_000:
                raise ValueError('catalog_too_large')
            if self.secrets.redact(catalog, config_references(config), additional=secrets.values()) != catalog:
                raise ValueError('catalog_contains_secret')
            fingerprint = content_hash({'config': config, 'secrets': secrets})
            with self.sessions.begin() as session:
                row = self._operation(session, identity, lock=True)
                if row.lease_owner != token or row.status != 'testing':
                    raise ValueError('test_expired')
                if row.cancel_requested:
                    row.status = 'cancelled'
                else:
                    row.status, row.error_code = 'tested', None
                    row.result = {**row.result, 'tools': catalog, 'tested_fingerprint': fingerprint}
                row.lease_owner, row.lease_until, row.updated_at = None, None, utcnow()
        except Exception:
            with self.sessions.begin() as session:
                row = self._operation(session, identity, lock=True)
                if row.lease_owner == token:
                    row.status = 'cancelled' if row.cancel_requested else 'failed'
                    row.error_code = 'connection_test_failed'
                    row.lease_owner, row.lease_until, row.updated_at = None, None, utcnow()
        return self.get(identity)

    def activate(self, identity: str, *, permissions: dict[str, str], enabled: bool) -> dict:
        """活动指针、撤销代次、工具许可与候选状态同事务提交，不能沿用新Schema的旧许可。"""
        with self.sessions.begin() as session:
            lock_control(session)
            row = self._operation(session, identity, lock=True)
            if row.status != 'tested' or row.cancel_requested:
                raise ValueError('候选必须先通过测试，再明确授权启用')
            current = self._current(session, row.target, row.expected_revision)
            config, tools = row.result['config'], row.result['tools']
            secrets = self.secrets.resolve(config_references(config))
            if content_hash({'config': config, 'secrets': secrets}) != row.result['tested_fingerprint']:
                raise ValueError('测试后凭据或配置已改变，请重新测试')
            if config.get('installation_id'):
                installation = session.get(MCPInstallation, config['installation_id'])
                if not installation or installation.status != 'prepared':
                    raise ValueError('候选安装已进入回收，请重新准备')
                self.storage.manifest(config['installation_id'])
            names = {tool['name'] for tool in tools}
            if set(permissions) != names or any(value not in {'deny', 'read', 'confirm'} for value in permissions.values()):
                raise ValueError('请逐项确认此候选全部工具的许可')
            if current is None:
                current = MCPConnection(id=row.target, name=row.parameters['name'], config=config)
                session.add(current)
            else:
                current.revision += 1
                current.authorization_epoch += 1
            current.name, current.config, current.enabled = row.parameters['name'], config, enabled
            current.tools = [{**tool, 'permission': permissions[tool['name']]} for tool in tools]
            MCPRegistry._save(session, current)
            row.status, row.updated_at, row.expires_at = 'active', utcnow(), None
            from knowledgeops_agent.harness.mcp.lifecycle import schedule
            schedule(session, row.target, name=current.name)
            return connection_view(current)

    def credentials(self, identity: str, *, values: dict[str, str] | None = None, connection=False,
                    expected_revision: int | None = None) -> builtins.list[dict]:
        """页面只读来源/是否配置；写入只接受此连接声明的变量，原值不回读。"""
        with self.sessions.begin() as session:
            lock_control(session)
            if connection:
                row = session.get(MCPConnection, identity)
                if not row or row.deleted:
                    raise KeyError('MCP连接不存在')
                if values is not None and row.revision != expected_revision:
                    raise ValueError('MCP配置修订已变化，请刷新')
                config = row.config
            else:
                operation = self._operation(session, identity, lock=True)
                if operation.status in {'active', 'removed', 'cancelled'}:
                    raise ValueError('请从活动连接管理凭据，或重新导入候选')
                config = operation.parameters['config']
            refs = config_references(config)
            if values is not None:
                if set(values) - refs:
                    raise ValueError('只能设置此连接已声明的环境引用')
                from knowledgeops_agent.harness.mcp.lifecycle import own_secret
                existing = self.secrets.project_values()
                for reference in values:
                    if reference not in existing:
                        own_secret(session, reference)
                self.secrets.update(values)
            return self.secrets.status(refs)

    def export_connection(self, identity: str) -> dict:
        row = MCPRegistry(self.sessions).get(identity)
        config = dict(row['config'])
        installation = config.pop('installation_id', None)
        if installation:
            with self.sessions() as session:
                record = session.get(MCPInstallation, installation)
                if not record:
                    raise ValueError('MCP安装记录缺失，不能导出不完整配置')
                request = record.request
            packages = request['packages']
            if request['manager'] == 'npm':
                prefix = (sum((['--package', package] for package in packages), []) + [request['entrypoint']]
                          if request.get('entrypoint') else [packages[0]])
                config['command'] = 'npx'
            else:
                prefix = ['--from', packages[0]] + sum((['--with', package] for package in packages[1:]), [])
                if request.get('python'):
                    prefix += ['--python', request['python']]
                prefix += [request['entrypoint']]
                config['command'] = 'uvx'
            config['args'] = [*prefix, *config.get('args', [])]
            if config.get('cwd') == str(self.storage.checked('data', identity)):
                config.pop('cwd')
        return {'name': row['name'], 'config': config}
