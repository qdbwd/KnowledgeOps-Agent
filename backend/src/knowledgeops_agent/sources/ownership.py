"""保存意图与内容去重分开；所有登记与删除沿用同一短事务控制锁。"""

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert

from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.persistence import models as db


def resolve_owner(session, *, conversation_id: str | None = None, root_id: str | None = None) -> tuple[str, str]:
    """调用入口决定归属，模型提供的标题/URL不能决定永久保存。

    已有Run沿真实会话归属；独立后台任务只登记其Run。旧无Run的业务调用仍可
    记录不透明运行身份，不能据此伪造会话，更不能自动提升成材料库资产。
    """
    if conversation_id and root_id:
        raise ValueError("choose conversation_id or root_id")
    if root_id:
        run = session.get(db.Run, root_id)
        if run is not None:
            if run.lifecycle_state != "active":
                raise ValueError("运行已封闭，不能追加材料")
            conversation_id = run.conversation_id
        if not conversation_id:
            return "run", root_id
    if conversation_id:
        conversation = session.get(db.Conversation, conversation_id)
        if conversation is None or conversation.deletion_state != "active":
            raise ValueError("会话已关闭或不存在，不能追加材料")
        return "conversation", conversation_id
    return "library", "default"


def claim_source(session, source_id: str, *, conversation_id: str | None = None,
                 root_id: str | None = None, allow_removed: bool = False) -> None:
    """与导入、选择或实际读取同事务提交；同一拥有者重复使用只留一条关系。"""
    lock_control(session)
    kind, identity = resolve_owner(session, conversation_id=conversation_id, root_id=root_id)
    source = session.get(db.Source, source_id)
    if source is None:
        raise KeyError("source not found")
    target = source_id + (":" + identity if kind == "conversation" else "")
    if kind in {"library", "conversation"} and session.scalar(select(db.CleanupJob.id).where(
        db.CleanupJob.target_kind == "source", db.CleanupJob.target_id == target,
        db.CleanupJob.status != "completed",
    ).limit(1)):
        raise ValueError("材料正在从这个列表移除，请等待清理完成后再保存")
    # 从材料库移除的固定版本仍可被旧引用读取；已有正文不因此重新出现在材料库。
    if source.deleted_at is not None and kind == "library":
        raise ValueError("材料已移除，不能重新保存旧身份；请重新导入")
    if source.deleted_at is not None and not allow_removed:
        raise KeyError("source not found")
    session.execute(insert(db.SourceUse).values(
        source_id=source_id, owner_kind=kind, owner_id=identity,
    ).on_conflict_do_nothing())


def library_predicate():
    saved = select(db.SourceUse.source_id).where(
        db.SourceUse.source_id == db.Source.id, db.SourceUse.owner_kind == "library",
    ).correlate(db.Source).exists()
    return or_(~db.Source.ownership_known, saved)


def scope_predicate(session, *, conversation_id=None, root_id=None, include_library=True):
    scopes = [library_predicate()] if include_library else []
    if conversation_id or root_id:
        kind, identity = resolve_owner(session, conversation_id=conversation_id, root_id=root_id)
        scopes.append(select(db.SourceUse.source_id).where(
            db.SourceUse.source_id == db.Source.id,
            and_(db.SourceUse.owner_kind == kind, db.SourceUse.owner_id == identity),
        ).correlate(db.Source).exists())
    return or_(*scopes) if scopes else False


def ownership_view(session, source) -> dict:
    return {
        "ownership_known": source.ownership_known,
        "independently_saved": bool(session.scalar(select(db.SourceUse.source_id).where(
            db.SourceUse.source_id == source.id, db.SourceUse.owner_kind == "library",
        ).limit(1))),
    }
