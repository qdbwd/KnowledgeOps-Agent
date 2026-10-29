"""MCP管理回收：先撤回绑定，再检查依赖；文件阶段可跨进程重试。

当前连接(含停用)、可继续的候选以及未决调用保护所需安装。历史修订保留
身份用于核对，不赋予重新启动旧程序的许可。文件删除不跨越控制事务。
"""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import cast, delete, func, or_, select
from sqlalchemy.dialects.postgresql import JSONPATH

from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.persistence import models as db

CANDIDATE = 'mcp_candidate'
CLEANUP = 'mcp_cleanup'
SECRET = 'mcp_secret'
PENDING = ('running', 'unknown', 'waiting_input')
LIVE = ('importing', 'imported', 'queued', 'preparing', 'prepared', 'testing', 'tested', 'failed', 'interrupted')


def schedule(session, target, *, name='', force=True):
    """在控制锁内登记；同一目标只推进一个清理意图，不要求Redis保留它。"""
    row = session.scalar(select(db.ExtensionOperation).where(
        db.ExtensionOperation.kind == CLEANUP, db.ExtensionOperation.target == target,
    ).order_by(db.ExtensionOperation.created_at.desc()).limit(1))
    if row is None:
        row = db.ExtensionOperation(kind=CLEANUP, target=target, status='queued', parameters={'name': name})
        session.add(row)
    elif row.status != 'running' and (force or row.status == 'completed'):
        row.status, row.lease_until, row.error_code = 'queued', None, None
        row.updated_at = db.utcnow()
    return row


def own_secret(session, reference):
    if not session.scalar(select(db.ExtensionOperation.id).where(
        db.ExtensionOperation.kind == SECRET, db.ExtensionOperation.target == reference,
        db.ExtensionOperation.status == 'owned',
    ).limit(1)):
        session.add(db.ExtensionOperation(kind=SECRET, target=reference, status='owned'))


def protections(session, target):
    """返回所需安装及服务目录的保护原因；未知旧回执保守保护整个服务。"""
    keep = set()
    current = session.get(db.MCPConnection, target)
    service_needed = bool(current and not current.deleted)
    if service_needed and current.config.get('installation_id'):
        keep.add(current.config['installation_id'])
    operations = list(session.scalars(select(db.ExtensionOperation).where(
        db.ExtensionOperation.kind == CANDIDATE, db.ExtensionOperation.target == target,
        db.ExtensionOperation.status.in_(LIVE),
    )))
    for operation in operations:
        service_needed = True
        if identity := operation.parameters.get('restore_from'):
            keep.add(identity)
        if (operation.status in {'queued', 'preparing', 'testing', 'prepared', 'tested'}
                or operation.result.get('config')):
            if identity := operation.result.get('installation_id'):
                keep.add(identity)
    uncertain = False
    calls = session.execute(select(db.MCPCall.connection_revision).where(
        db.MCPCall.connection_id == target, db.MCPCall.status.in_(PENDING),
    ).distinct()).scalars()
    for revision in calls:
        service_needed = True
        binding = session.get(db.MCPConnectionRevision, (target, revision))
        if binding is None:
            uncertain = True
        elif identity := binding.content.get('config', {}).get('installation_id'):
            keep.add(identity)
    return keep, service_needed, uncertain


def _uses(config, reference):
    variables = func.jsonb_build_object('ref', reference)
    return or_(func.jsonb_path_exists(config, cast('$.environment_refs.* ? (@ == $ref)', JSONPATH), variables),
               func.jsonb_path_exists(config, cast('$.header_refs.* ? (@ == $ref)', JSONPATH), variables))


def secret_in_use(session, reference):
    if session.scalar(select(db.MCPConnection.id).where(db.MCPConnection.deleted.is_(False),
        _uses(db.MCPConnection.config, reference)).limit(1)):
        return True
    if session.scalar(select(db.ExtensionOperation.id).where(db.ExtensionOperation.kind == CANDIDATE,
        db.ExtensionOperation.status.in_(LIVE), _uses(db.ExtensionOperation.parameters['config'], reference)).limit(1)):
        return True
    return bool(session.scalar(select(db.MCPCall.id).join(db.MCPConnectionRevision,
        (db.MCPCall.connection_id == db.MCPConnectionRevision.connection_id)
        & (db.MCPCall.connection_revision == db.MCPConnectionRevision.revision)).where(
            db.MCPCall.status.in_(PENDING), _uses(db.MCPConnectionRevision.content['config'], reference)).limit(1)))


class MCPCleaner:
    def __init__(self, manager):
        self.manager, self.sessions, self.storage = manager, manager.sessions, manager.storage

    def list(self, limit=30):
        with self.sessions() as session:
            return [{'id': row.id, 'target': row.target, 'name': row.parameters.get('name', ''),
                     'status': row.status, 'result': row.result, 'error_code': row.error_code}
                    for row in session.scalars(select(db.ExtensionOperation).where(
                        db.ExtensionOperation.kind == CLEANUP).order_by(db.ExtensionOperation.updated_at.desc()).limit(limit))]

    def get(self, identity):
        """全局活动可直接定位旧清理记录，不受最近列表的条数限制。"""
        with self.sessions() as session:
            row = session.get(db.ExtensionOperation, identity)
            if not row or row.kind != CLEANUP:
                raise KeyError('MCP清理记录不存在')
            return {'id': row.id, 'target': row.target, 'name': row.parameters.get('name', ''),
                    'status': row.status, 'result': row.result, 'error_code': row.error_code}

    def retry(self, identity):
        with self.sessions.begin() as session:
            lock_control(session)
            row = session.get(db.ExtensionOperation, identity)
            if not row or row.kind != CLEANUP:
                raise KeyError('MCP清理记录不存在')
            if row.status == 'running' and row.lease_until and row.lease_until > db.utcnow():
                raise ValueError('清理正在执行')
            row.status, row.error_code, row.lease_until = 'queued', None, None
            row.updated_at = db.utcnow()

    def reconcile(self, *, expire=True, limit=20):
        """滚动检查安装登记，兼容旧候选/被中断的管理请求；不枚举磁盘猜测归属。"""
        with self.sessions.begin() as session:
            lock_control(session)
            if expire:
                for row in session.scalars(select(db.ExtensionOperation).where(
                    db.ExtensionOperation.kind == CANDIDATE, db.ExtensionOperation.status.in_(LIVE),
                    db.ExtensionOperation.status.notin_(['importing', 'preparing', 'testing']),
                    db.ExtensionOperation.expires_at <= db.utcnow(),
                ).limit(limit)):
                    row.status, row.cancel_requested, row.updated_at = 'expired', True, db.utcnow()
                    schedule(session, row.target, name=row.parameters.get('name', ''))
            state = session.get(db.MaintenanceState, 'mcp-installation-scan')
            if state is None:
                state = db.MaintenanceState(name='mcp-installation-scan', cursor={})
                session.add(state)
            after = state.cursor.get('after', '')
            records = list(session.scalars(select(db.MCPInstallation).where(db.MCPInstallation.id > after,
                db.MCPInstallation.status != 'removed').order_by(db.MCPInstallation.id).limit(limit)))
            for record in records:
                keep, _, uncertain = protections(session, record.connection_id)
                if record.id not in keep and not uncertain:
                    schedule(session, record.connection_id, force=False)
            state.cursor = {'after': records[-1].id if len(records) == limit else ''}

    def step(self, *, limit=20):
        token = str(uuid4())
        with self.sessions.begin() as session:
            lock_control(session)
            job = session.scalar(select(db.ExtensionOperation).where(db.ExtensionOperation.kind == CLEANUP,
                db.ExtensionOperation.status != 'completed',
                or_(db.ExtensionOperation.lease_until.is_(None), db.ExtensionOperation.lease_until <= db.utcnow()),
            ).order_by(db.ExtensionOperation.updated_at, db.ExtensionOperation.id).limit(1).with_for_update(skip_locked=True))
            if job is None:
                return None
            job.status, job.lease_owner = 'running', token
            job.lease_until = db.utcnow() + timedelta(minutes=5)
            job.attempts += 1
            identity, target = job.id, job.target
            keep, _, uncertain = protections(session, target)
            records = list(session.scalars(select(db.MCPInstallation).where(db.MCPInstallation.connection_id == target,
                db.MCPInstallation.status != 'removed', db.MCPInstallation.id.notin_(keep),
            ).order_by(db.MCPInstallation.created_at, db.MCPInstallation.id).limit(limit)))
            eligible = []
            for record in records:
                if record.id not in keep and not uncertain:
                    record.status, record.updated_at = 'removing', db.utcnow()
                    eligible.append((record.id, record.operation_id))
        # PG先标记不可再激活；慢文件处理不持全局控制锁。使用锁不能获得时等下轮。
        errors, busy, removed = [], False, 0
        for installation, operation in eligible:
            try:
                if not self.storage.remove(installation, connection=target, operation=operation):
                    busy = True
                    continue
                with self.sessions.begin() as session:
                    record = session.get(db.MCPInstallation, installation)
                    record.status, record.removed_at, record.updated_at = 'removed', db.utcnow(), db.utcnow()
                removed += 1
            except (OSError, ValueError):
                errors.append('owned_files_unavailable')
        with self.sessions.begin() as session:
            lock_control(session)
            _, service_needed, uncertain = protections(session, target)
            remaining = bool(session.scalar(select(db.MCPInstallation.id).where(
                db.MCPInstallation.connection_id == target, db.MCPInstallation.status != 'removed').limit(1)))
        # 已移除连接或已放弃新候选无法再以此身份创建新版本；服务目录只在所有安装释放后处理。
        if not service_needed and not remaining and not uncertain:
            for area in ('cache', 'data'):
                try:
                    self.storage.remove_service(area, target)
                except (OSError, ValueError):
                    errors.append('owned_files_unavailable')
        with self.sessions.begin() as session:
            job = session.get(db.ExtensionOperation, identity)
            if job.lease_owner != token:
                return {'status': 'lease_lost'}
            # 当前连接/可用候选仍需要的文件无须等待清理；之后的更新/删除会重新登记。
            keep, _, uncertain = protections(session, target)
            pending = bool(session.scalar(select(db.MCPInstallation.id).where(
                db.MCPInstallation.connection_id == target, db.MCPInstallation.status != 'removed',
                db.MCPInstallation.id.notin_(keep)).limit(1)))
            current = session.get(db.MCPConnection, target)
            unresolved_service = not (current and not current.deleted) and service_needed
            job.status = 'failed' if errors else 'waiting' if busy or pending or uncertain or unresolved_service else 'completed'
            job.error_code = errors[0] if errors else None
            job.result = {'removed_installations': job.result.get('removed_installations', 0) + removed,
                          'retained_installations': len(keep),
                          'waiting_for_use': busy or uncertain or pending or unresolved_service}
            job.lease_owner, job.updated_at = None, db.utcnow()
            job.lease_until = db.utcnow() + timedelta(seconds=30) if job.status != 'completed' else None
            return {'id': identity, 'status': job.status, **job.result}

    def secrets(self, *, limit=20):
        with self.sessions.begin() as session:
            lock_control(session)
            rows = list(session.scalars(select(db.ExtensionOperation).where(db.ExtensionOperation.kind == SECRET,
                db.ExtensionOperation.status == 'owned').order_by(db.ExtensionOperation.updated_at).limit(limit)))
            removed = 0
            for row in rows:
                if not secret_in_use(session, row.target):
                    # 只删除由页面/导入明确创建的项目变量。系统环境和手工变量不取得删除权。
                    self.manager.secrets.update({row.target: None})
                    row.status, row.updated_at = 'removed', db.utcnow()
                    removed += 1
                else:
                    row.updated_at = db.utcnow()  # 有界轮转，不能让前20个共享变量饿死后续清理。
            return removed

    def prune(self, *, retention_days=30, limit=20):
        cutoff = db.utcnow() - timedelta(days=max(1, retention_days))
        with self.sessions.begin() as session:
            lock_control(session)
            # 历史安装仍可由回执/冻结配置定位；只回收大日志，保留最小安装描述。
            rows = list(session.scalars(select(db.ExtensionOperation).where(db.ExtensionOperation.kind == CANDIDATE,
                db.ExtensionOperation.status.in_(['active', 'removed', 'expired', 'cancelled']),
                db.ExtensionOperation.updated_at < cutoff).limit(limit)))
            for row in rows:
                row.status = 'archived'
                row.parameters = {'name': row.parameters.get('name', '')}
                row.result = {}
                row.updated_at = db.utcnow()
            stale = list(session.scalars(select(db.MCPInstallation).where(db.MCPInstallation.status == 'removed',
                db.MCPInstallation.removed_at < cutoff,
                ~select(db.MCPConnection.id).where(
                    db.MCPConnection.config['installation_id'].astext == db.MCPInstallation.id).exists(),
                ~select(db.MCPConnectionRevision.connection_id).where(
                    db.MCPConnectionRevision.content['config']['installation_id'].astext == db.MCPInstallation.id).exists(),
                ~select(db.RunResourceSnapshot.root_id).where(db.RunResourceSnapshot.content['mcp'].contains(
                    func.jsonb_build_array(func.jsonb_build_object('config',
                        func.jsonb_build_object('installation_id', db.MCPInstallation.id))))).exists(),
            ).limit(limit)))
            for installation in stale:
                # 原worker代次已不可能再通过检查；过了保留期才释放小型文件墓碑和安装身份。
                self.storage.forget(installation.id)
                session.delete(installation)
            session.flush()
            disposable = list(session.scalars(select(db.ExtensionOperation).where(
                db.ExtensionOperation.kind.in_([CLEANUP, SECRET, CANDIDATE]),
                db.ExtensionOperation.status.in_(['completed', 'removed', 'archived']),
                db.ExtensionOperation.updated_at < cutoff,
                ~select(db.MCPInstallation.id).where(db.MCPInstallation.operation_id == db.ExtensionOperation.id).exists(),
            ).limit(limit)))
            for operation in disposable:
                if operation.kind == CLEANUP:
                    _, service_needed, uncertain = protections(session, operation.target)
                    remaining = session.scalar(select(db.MCPInstallation.id).where(
                        db.MCPInstallation.connection_id == operation.target,
                        db.MCPInstallation.status != 'removed').limit(1))
                    if not service_needed and not uncertain and not remaining:
                        self.storage.forget_service(operation.target)
            session.execute(delete(db.ExtensionOperation).where(db.ExtensionOperation.id.in_([row.id for row in disposable])))
            return len(rows) + len(stale) + len(disposable)

    def maintain(self, *, expire=True, retention_days=30):
        interrupted = self.manager.recover_expired()
        self.reconcile(expire=expire)
        result = {'interrupted': interrupted, 'cleanup': self.step()}
        try:
            result['secrets_removed'] = self.secrets()
        except (OSError, ValueError):
            result['secret_error'] = 'environment_unavailable'
        if expire:
            result['pruned'] = self.prune(retention_days=retention_days)
        return result
