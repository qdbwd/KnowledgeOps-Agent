"""旧结构化引用的有界回填。先完成全表巡检，再允许正文垃圾回收。"""

from sqlalchemy import delete, event, select, tuple_
from sqlalchemy.dialects.postgresql import insert

from knowledgeops_agent.app.lifecycle.references import replace_references
from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.persistence import models as db

CATALOG_STATE = "artifact-references-v2"

# 身份格式与在线保存路径一致；额外资产沿明确所属类型命名，避免互相覆盖。
SOURCES = (
    (db.ChatMessage, "message", ""),
    (db.ContinuationSnapshot, "snapshot", ""),
    (db.TaskHistoryRecord, "history", ""),
    (db.ChangeSetRevision, "draft", ""),
    (db.ChangeSet, "draft", "current:"),
    (db.KnowledgeVersion, "asset", "knowledge:"),
    (db.SourceSegment, "asset", "source-segment:"),
    (db.PreferenceRevision, "asset", "memory:"),
    (db.LearningTaskRevision, "asset", "learning:"),
    (db.LearningTask, "asset", "learning-current:"),
    (db.LearningUnit, "asset", "learning-unit:"),
    (db.LearningProfileRevision, "asset", "profile:"),
    (db.LearningProfileImport, "asset", "profile-import:"),
    (db.LearningCollaboration, "asset", "collaboration:"),
    (db.RunArtifact, "material", ""),
    (db.Run, "run_input", ""),
)


def reference_value(session, row):
    """读取正式正文而非只读路径字段；文件缺失/哈希不符时回填失败并保护材料。"""
    if isinstance(row, db.ChatMessage):
        return row.content  # 隐藏协议不是用户引用，不加入正文保留关系。
    if isinstance(row, db.ContinuationSnapshot):
        return row.content
    if isinstance(row, db.TaskHistoryRecord):
        return row.payload
    if isinstance(row, db.ChangeSetRevision):
        return row.operations
    if isinstance(row, db.KnowledgeVersion):
        return row.body
    if isinstance(row, db.RunArtifact):
        return row.content if row.expired_at is None else ""
    if isinstance(row, db.Run):
        return {"task": row.task, "input": row.input_data, "outcome": row.outcome} if row.lifecycle_state == "active" else {}
    if isinstance(row, db.SourceSegment):
        # 分段可能恰好切开内部链接，只看单段会漏掉UUID/地址；邻接片段仅补边界。
        previous = session.scalar(select(db.SourceSegment.text).where(
            db.SourceSegment.source_version_id == row.source_version_id,
            db.SourceSegment.end_offset == row.start_offset, db.SourceSegment.id != row.id,
        ).order_by(db.SourceSegment.position.desc()).limit(1))
        following = session.scalar(select(db.SourceSegment.text).where(
            db.SourceSegment.source_version_id == row.source_version_id,
            db.SourceSegment.start_offset == row.end_offset, db.SourceSegment.id != row.id,
        ).order_by(db.SourceSegment.position).limit(1))
        return (previous or "")[-128:] + row.text + (following or "")[:128]
    value = {attribute.key: getattr(row, attribute.key) for attribute in row.__mapper__.column_attrs}
    if isinstance(row, (db.LearningProfileRevision, db.LearningProfileImport, db.LearningCollaboration)):
        store = session.info.get("file_store")
        if store is None:
            raise ValueError("reference backfill requires the configured workspace")
        digest = row.material_hash if isinstance(row, db.LearningCollaboration) else row.sha256
        value["body"] = store.read(row.storage_key, digest)
    return value


def reference_identity(row):
    for model, kind, prefix in SOURCES:
        if isinstance(row, model):
            key = prefix + ":".join(str(getattr(row, str(column.key))) for column in model.__mapper__.primary_key)
            return kind, key
    return None


def install_reference_tracking(sessions) -> None:
    """只为本应用Session工厂安装事务内追踪，不全局改变SQLAlchemy行为。

    ORM创建/编辑的资产在flush后已有稳定ID和文件版本；引用与资产一起提交或
    回滚。原有Core批量历史、材料入口仍显式维护引用，不能依赖ORM事件覆盖它们。
    """
    if event.contains(sessions, "before_flush", _capture_changes):
        return
    event.listen(sessions, "before_flush", _capture_changes)
    event.listen(sessions, "after_flush_postexec", _track_changes)
    event.listen(sessions, "after_soft_rollback", _discard_changes)


def _capture_changes(session, _context, _instances):
    changed = [row for row in (*session.new, *session.dirty, *session.deleted)
               if reference_identity(row) is not None]
    if changed:
        lock_control(session)
        session.info["lifecycle_reference_changes"] = [(row, row in session.deleted) for row in changed]


def _track_changes(session, _context):
    for row, removed in session.info.pop("lifecycle_reference_changes", []):
        kind, key = reference_identity(row)
        if removed:
            session.execute(delete(db.ArtifactRetentionReference).where(
                db.ArtifactRetentionReference.owner_kind == kind,
                db.ArtifactRetentionReference.owner_id == key,
            ))
        else:
            replace_references(session, kind, key, reference_value(session, row), snapshot=kind == "snapshot")


def _discard_changes(session, _transaction):
    session.info.pop("lifecycle_reference_changes", None)


class ReferenceCatalog:
    def __init__(self, sessions):
        self.sessions = sessions

    @staticmethod
    def ready(session) -> bool:
        state = session.get(db.MaintenanceState, CATALOG_STATE)
        return bool(state and state.cursor.get("complete"))

    def step(self, *, limit: int = 100) -> dict:
        """每次一张表的一批，记录复合主键游标；进程重启无需重新扫描已经核对的正文。"""
        if not 1 <= limit <= 1000:
            raise ValueError("invalid reference backfill batch size")
        with self.sessions.begin() as session:
            lock_control(session)
            session.execute(insert(db.MaintenanceState).values(
                name=CATALOG_STATE, cursor={}, next_scan_at=db.utcnow(), updated_at=db.utcnow(),
            ).on_conflict_do_nothing())
            state = session.get(db.MaintenanceState, CATALOG_STATE)
            if state.cursor.get("complete"):
                return {"processed": 0, "complete": True}
            index = int(state.cursor.get("table", 0))
            model, kind, prefix = SOURCES[index]
            columns = list(model.__mapper__.primary_key)
            query = select(model)
            if state.cursor.get("after"):
                query = query.where(tuple_(*columns) > tuple_(*state.cursor["after"]))
            rows = list(session.scalars(query.order_by(*columns).limit(limit + 1)))
            for row in rows[:limit]:
                key = prefix + ":".join(str(getattr(row, str(column.key))) for column in columns)
                replace_references(session, kind, key, reference_value(session, row), snapshot=kind == "snapshot")
            if len(rows) > limit:
                last = rows[limit - 1]
                state.cursor = {"table": index, "after": [getattr(last, str(column.key)) for column in columns]}
            else:
                state.cursor = {"table": index + 1, "complete": index + 1 == len(SOURCES)}
            state.updated_at = db.utcnow()
            return {"processed": min(len(rows), limit), "complete": state.cursor.get("complete", False)}
