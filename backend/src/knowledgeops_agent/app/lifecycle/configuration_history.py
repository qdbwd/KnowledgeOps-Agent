"""能力配置历史的有界回收；保留当前指针、冻结执行与业务核对需要的修订。"""

from datetime import timedelta

from sqlalchemy import delete, func, literal, or_, select, tuple_
from sqlalchemy.sql import ColumnElement

from knowledgeops_agent.harness.tools.configuration import lock_configuration
from knowledgeops_agent.persistence import models as db

TABLES: dict[str, tuple[
    type[db.SubagentDefinitionRevision | db.ToolPolicyRevision | db.ToolGroupRevision],
    type[db.SubagentDefinition | db.ToolPolicy | db.ToolGroup], str,
]] = {
    "subagent": (db.SubagentDefinitionRevision, db.SubagentDefinition, "definition_id"),
    "tool_policy": (db.ToolPolicyRevision, db.ToolPolicy, "policy_id"),
    "tool_group": (db.ToolGroupRevision, db.ToolGroup, "group_id"),
}
KINDS = tuple(TABLES)


def configuration_history_batch(session, kind: str, *, now, retention_days=30,
                                after=None, limit=200, execute=False) -> dict:
    """查询和删除共用资格条件。持配置锁而不取Run锁，避免与首次冻结的锁顺序反转。

    每批只读取身份、创建时间和依赖布尔值；JSON绑定由PG比较，不加载整份Skills
    或全部角色正文。新快照的共享配置锁与本删除互斥，旧快照不会被改成当前配置。
    """
    if kind not in TABLES or not 1 <= limit <= 1000 or retention_days < 1:
        raise ValueError("无效的配置历史种类、批次或保留期限")
    if after is not None and (not isinstance(after, (tuple, list)) or len(after) != 2
            or not isinstance(after[0], str) or not after[0] or len(after[0]) > 100
            or type(after[1]) is not int or after[1] < 0):
        raise ValueError("配置历史游标必须为[身份,非负修订号]")
    lock_configuration(session, shared=not execute)
    revision, current, owner = TABLES[kind]
    identity = getattr(revision, owner)
    binding = func.jsonb_build_object("id", identity, "revision", revision.revision)
    is_current = select(current.id).where(
        current.id == identity, current.revision == revision.revision,
    ).correlate(revision).exists()
    referenced: ColumnElement[bool] = literal(False)
    if kind == "subagent":
        referenced = or_(
            select(db.RunResourceSnapshot.root_id).where(
                db.RunResourceSnapshot.content["subagents"].contains(func.jsonb_build_array(binding)),
            ).correlate(revision).exists(),
            select(db.Run.id).where(db.Run.input_data["subagent"].contains(binding)).correlate(revision).exists(),
            # 真实审读通过记录仍可能参与后续发布，即使对应运行详情已经清理。
            select(db.LearningCollaboration.id).where(
                db.LearningCollaboration.input_manifest["definition"].contains(binding),
            ).correlate(revision).exists(),
        )
    elif kind == "tool_policy":
        referenced = select(db.RunResourceSnapshot.root_id).where(
            db.RunResourceSnapshot.content["tool_policy"].contains(binding),
        ).correlate(revision).exists()
    # 组在保存策略/角色时已展开，不存在依靠历史组成员重新计算的运行绑定。
    query = select(identity.label("id"), revision.revision, revision.created_at,
                   is_current.label("current"), referenced.label("referenced"))
    if after is not None:
        query = query.where(tuple_(identity, revision.revision) > tuple(after))
    rows = session.execute(query.order_by(identity, revision.revision).limit(limit + 1)).mappings().all()
    cutoff = now - timedelta(days=retention_days)
    items = []
    for row in rows[:limit]:
        reason = "current" if row["current"] else "referenced" if row["referenced"] else (
            "retention_period" if row["created_at"] > cutoff else "unreferenced")
        items.append({"id": row["id"], "revision": row["revision"],
                      "created_at": row["created_at"].isoformat(), "reason": reason,
                      "eligible": reason == "unreferenced"})
    candidates = [(row["id"], row["revision"]) for row in items if row["eligible"]]
    if execute and candidates:
        session.execute(delete(revision).where(tuple_(identity, revision.revision).in_(candidates)))
    return {"kind": kind, "mode": "execute" if execute else "preview", "items": items,
            "scanned": len(items), "eligible": len(candidates),
            "removed": len(candidates) if execute else 0,
            "next_cursor": [items[-1]["id"], items[-1]["revision"]] if len(rows) > limit else None}


class ConfigurationHistoryCleaner:
    def __init__(self, sessions, *, now=db.utcnow, retention_days=30):
        self.sessions, self.now, self.retention_days = sessions, now, retention_days

    def preview(self, kind: str, *, after=None, limit=200) -> dict:
        with self.sessions.begin() as session:
            return configuration_history_batch(session, kind, now=self.now(),
                retention_days=self.retention_days, after=after, limit=limit)

    def step(self, kind: str, *, after=None, limit=200) -> dict:
        with self.sessions.begin() as session:
            return configuration_history_batch(session, kind, now=self.now(),
                retention_days=self.retention_days, after=after, limit=limit, execute=True)
