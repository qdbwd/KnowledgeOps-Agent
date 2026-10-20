"""可移植扩展备份：完整技能字节、MCP锁和可选数据；不复制秘密或依赖环境。

调用者须先停止应用写入，并把此归档与PG/工作区纳入同一份校验清单。
恢复只写新目录/隔离数据库，撤回旧机器的许可并产生待明确准备的候选。
"""

import hashlib
import io
import json
import os
import stat
import tarfile
from datetime import timedelta
from pathlib import Path, PurePosixPath

from sqlalchemy import inspect, select

from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.harness.mcp.installations import FORMAT as MCP_FORMAT
from knowledgeops_agent.harness.mcp.installations import MCPInstallStorage
from knowledgeops_agent.harness.mcp.lifecycle import LIVE
from knowledgeops_agent.harness.mcp.registry import MCPRegistry
from knowledgeops_agent.persistence import models as db

FORMAT = 'knowledgeops-extensions-v1'


def _tree(root):
    """归档不跟随链接；文件包和服务数据只能是普通目录/文件。"""
    if not root.exists():
        return
    if root.is_symlink() or any(path.is_symlink() for path in root.parents):
        raise ValueError('扩展备份根不能经过链接')
    for current, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            if (Path(current) / name).is_symlink():
                raise ValueError('扩展备份不能包含目录链接')
        for name in sorted(files):
            path = Path(current) / name
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError('扩展备份不能包含链接或特殊文件')
            yield path


def create_extensions(sessions, *, skills: Path, mcp: Path, output: Path, include_data=False):
    storage = MCPInstallStorage(mcp)
    hashes: dict[str, str] = {}
    installations, connections = {}, set()
    with sessions() as session:
        # 正式0039升级前也必须能备份，旧库没有安装表。
        if inspect(session.connection()).has_table('mcp_installations'):
            for row in session.scalars(select(db.MCPInstallation).where(
                db.MCPInstallation.status.in_(['prepared', 'restore_required']))):
                manifest, locks = storage.portable(row.id)
                if (manifest['connection_id'] != row.connection_id or manifest['operation_id'] != row.operation_id
                        or manifest['request'] != row.request):
                    raise ValueError('MCP备份安装清单与数据库不符')
                installations[row.id] = {'manifest': manifest, 'locks': locks}
                connections.add(row.connection_id)
        if inspect(session.connection()).has_table('mcp_connections'):
            connections.update(session.scalars(select(db.MCPConnection.id)))
        if inspect(session.connection()).has_table('extension_operations'):
            connections.update(session.scalars(select(db.ExtensionOperation.target).where(
                db.ExtensionOperation.kind == 'mcp_candidate')))
    manifest = {'format': FORMAT, 'source_mcp_root': str(storage.root), 'files': hashes,
                'installations': installations, 'include_data': include_data, 'data_connections': []}
    with tarfile.open(output, 'x:gz') as archive:
        def add(path, name):
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('扩展归档对象不是普通文件')
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                stream.seek(0)
                entry = tarfile.TarInfo(name)
                entry.size, entry.mode = info.st_size, stat.S_IMODE(info.st_mode) & 0o777
                archive.addfile(entry, stream)
                hashes[name] = digest
        for path in _tree(skills):
            add(path, 'skills/' + path.relative_to(skills).as_posix())
        if include_data:
            for connection in sorted(connections):
                directory = storage.checked('data', connection)
                if not directory.exists():
                    continue
                expected = {'format': MCP_FORMAT, 'connection_id': connection, 'area': 'data'}
                if json.loads((directory / 'owner.json').read_text()) != expected:
                    raise ValueError('MCP服务数据缺少有效归属')
                manifest['data_connections'].append(connection)
                for path in _tree(directory):
                    add(path, f'mcp/data/{connection}/' + path.relative_to(directory).as_posix())
        payload = json.dumps(manifest, ensure_ascii=False).encode()
        entry = tarfile.TarInfo('extensions.json')
        entry.size, entry.mode = len(payload), 0o600
        archive.addfile(entry, io.BytesIO(payload))
    return {'skill_files': sum(name.startswith('skills/') for name in hashes),
            'installations': len(installations), 'data_connections': len(manifest['data_connections'])}


def restore_extensions(sessions, *, archive: Path, target: Path):
    """先完整校验，再展开新目录；不连接MCP、不安装依赖、不触发队列。"""
    target = target.absolute()
    if target.exists() or any(path.is_symlink() for path in (target, *target.parents)):
        raise ValueError('扩展只能恢复到不存在的新目录')
    with tarfile.open(archive) as packed:
        members = packed.getmembers()
        names = [item.name for item in members]
        if len(names) != len(set(names)) or 'extensions.json' not in names:
            raise ValueError('扩展归档缺少清单或包含重名')
        for item in members:
            path = PurePosixPath(item.name)
            if (not item.isfile() or path.is_absolute() or '..' in path.parts or '\\' in item.name
                    or str(path) != item.name or item.name.startswith('/')):
                raise ValueError('扩展归档包含非法路径或链接')
        header = packed.getmember('extensions.json')
        if header.size > 32 * 1024 * 1024:
            raise ValueError('扩展归档清单过大')
        stream = packed.extractfile(header)
        assert stream is not None  # 上方已经拒绝所有非普通文件。
        with stream:
            manifest = json.load(stream)
        if manifest.get('format') != FORMAT or set(names) != {'extensions.json', *manifest['files']}:
            raise ValueError('扩展归档清单不完整')
        for name, digest in manifest['files'].items():
            if not name.startswith(('skills/', 'mcp/data/')):
                raise ValueError('扩展归档包含不允许的文件类型')
            stream = packed.extractfile(name)
            assert stream is not None
            with stream:
                actual = hashlib.sha256()
                while block := stream.read(1024 * 1024):
                    actual.update(block)
                if actual.hexdigest() != digest:
                    raise ValueError('扩展归档文件摘要不符')
        target.mkdir(parents=True, mode=0o700)
        packed.extractall(target, filter='data')
    storage = MCPInstallStorage(target / 'mcp')
    old_root = Path(manifest['source_mcp_root'])

    def relocated(config, connection):
        result = dict(config)
        if result.get('cwd') == str(old_root / 'data' / connection):
            result['cwd'] = str(storage.checked('data', connection))
        identity = result.get('installation_id')
        if identity:
            if identity not in manifest['installations']:
                raise ValueError('活动MCP安装没有可恢复锁文件')
            result['command'] = str(storage.release(identity) / manifest['installations'][identity]['manifest']['entrypoint'])
        return result

    with sessions.begin() as session:
        lock_control(session)
        for identity, item in manifest['installations'].items():
            row = session.get(db.MCPInstallation, identity)
            info = item['manifest']
            if (not row or info['connection_id'] != row.connection_id or info['operation_id'] != row.operation_id
                    or info['request'] != row.request):
                raise ValueError('恢复的MCP锁文件与数据库不是同一备份')
            directory = storage.create(row.connection_id, identity, row.operation_id)
            # 清单只允许npm/uv的固定锁文件名，不能由备份指定任意写入路径。
            allowed = {'package.json', 'package-lock.json'} if row.request['manager'] == 'npm' else {'requirements.in', 'requirements.lock'}
            if set(item['locks']) != allowed or set(info['lock_files']) != allowed:
                raise ValueError('恢复锁文件不完整')
            for name, value in item['locks'].items():
                if hashlib.sha256(value.encode()).hexdigest() != info['lock_files'][name]:
                    raise ValueError('恢复锁文件摘要不符')
                (directory / name).write_text(value, encoding='utf-8')
            storage.complete(identity, info)
            row.status, row.updated_at = 'restore_required', db.utcnow()
        for connection in {*manifest['data_connections'], *(item['manifest']['connection_id'] for item in manifest['installations'].values())}:
            storage.service_directory('data', connection)
        current = list(session.scalars(select(db.MCPConnection).where(db.MCPConnection.deleted.is_(False))))
        revisions = {}
        for row in current:
            config = relocated(row.config, row.id)
            row.config, row.enabled, row.tools = config, False, []
            row.revision, row.authorization_epoch = row.revision + 1, row.authorization_epoch + 1
            MCPRegistry._save(session, row)
            revisions[row.id] = row.revision
            config = dict(config)
            identity = config.pop('installation_id', None)
            request = session.get(db.MCPInstallation, identity).request if identity else None
            if identity:
                config['command'] = None
                if config.get('cwd') == str(storage.checked('data', row.id)):
                    config['cwd'] = None  # 实际准备时按当前挂载重新定位受管数据目录。
            session.add(db.ExtensionOperation(kind='mcp_candidate', target=row.id,
                expected_revision=row.revision, status='imported' if request else 'prepared',
                parameters={'name': row.name, 'config': config, 'installation': request,
                            **({'restore_from': identity} if identity else {})},
                result={} if request else {'config': config}, expires_at=db.utcnow() + timedelta(days=7)))
        # 未完成候选没有下载授权可自动重放；仅恢复为可由用户继续的明确状态。
        for operation in session.scalars(select(db.ExtensionOperation).where(
            db.ExtensionOperation.kind == 'mcp_candidate', db.ExtensionOperation.status.in_(LIVE))):
            config = relocated(operation.parameters['config'], operation.target)
            identity = operation.result.get('installation_id')
            parameters = {**operation.parameters, 'config': config}
            if identity in manifest['installations']:
                parameters['restore_from'] = identity
            request = parameters.get('installation')
            if request and config.get('cwd') == str(storage.checked('data', operation.target)):
                config['cwd'] = None
            operation.parameters, operation.result = parameters, {} if request else {'config': config}
            operation.status = 'imported' if request else 'prepared'
            operation.expected_revision = revisions.get(operation.target, operation.expected_revision)
            operation.lease_owner, operation.lease_until, operation.cancel_requested = None, None, False
            operation.error_code, operation.updated_at = 'restored_requires_review', db.utcnow()
    return {'files_verified': len(manifest['files']), 'installations_requiring_preparation': len(manifest['installations']),
            'connections_disabled': len(current), 'data_included': manifest['include_data'],
            'secrets_restored': False, 'dependencies_installed': False}
