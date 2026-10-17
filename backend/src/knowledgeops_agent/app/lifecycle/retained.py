"""会话延续与可达历史的保留计算；访问目录不自动钉住所有原文。"""

from sqlalchemy import delete, select, update

from knowledgeops_agent.app.lifecycle.references import replace_references
from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.harness.context.history import _digest, visible_records
from knowledgeops_agent.persistence import models as db


def root_history_scopes(session, root) -> set[str]:
    """历史查询会保存未归档原话；不能只靠尚存批次找回需要核对的作用域。"""
    owners = select(db.Run.id).where(db.Run.root_id == root.id)
    scopes = set(session.scalars(select(db.TaskHistoryBatch.scope).where(
        db.TaskHistoryBatch.run_id.in_(owners),
    ).distinct()))
    scopes.add(_digest(["conversation", root.conversation_id]))
    children = session.scalars(select(db.Run.id).where(db.Run.root_id == root.id, db.Run.id != root.id))
    scopes.update(_digest(["child", identity]) for identity in children)
    return scopes


def prune_snapshots(session, conversation_id: str, *, limit: int = 100) -> dict:
    """保留当前指针和任意可恢复运行的绑定，祖先关系只用于追溯而不保留全文。

    与入队/领取/快照提交共用控制锁。快照正文和hash不改写，仅解除已回收祖先
    的追溯指针；旧运行仍绑定的不可变快照不得被换成当前最新快照。
    """
    if not 1 <= limit <= 1000:
        raise ValueError("invalid snapshot batch size")
    lock_control(session)
    conversation = session.get(db.Conversation, conversation_id)
    if conversation is None:
        raise KeyError("conversation not found")
    bound = select(db.Run.continuation_snapshot_id).where(
        db.Run.lifecycle_state == "active", db.Run.continuation_snapshot_id.is_not(None),
    )
    owners = select(db.Run.id).where(db.Run.lifecycle_state.in_(["cleaning", "expired"]))
    query = select(db.ContinuationSnapshot.id).where(
        db.ContinuationSnapshot.conversation_id == conversation_id,
        db.ContinuationSnapshot.run_id.in_(owners),
        db.ContinuationSnapshot.id.not_in(bound),
    )
    if conversation.latest_snapshot_id:
        query = query.where(db.ContinuationSnapshot.id != conversation.latest_snapshot_id)
    identities = list(session.scalars(query.order_by(db.ContinuationSnapshot.id).limit(limit + 1)))
    batch = identities[:limit]
    if batch:
        session.execute(update(db.Run).where(
            db.Run.lifecycle_state != "active", db.Run.continuation_snapshot_id.in_(batch),
        ).values(continuation_snapshot_id=None))
        session.execute(update(db.ContinuationSnapshot).where(
            db.ContinuationSnapshot.parent_snapshot_id.in_(batch),
        ).values(parent_snapshot_id=None))
        session.execute(delete(db.ArtifactRetentionReference).where(
            db.ArtifactRetentionReference.owner_kind == "snapshot",
            db.ArtifactRetentionReference.owner_id.in_(batch),
        ))
        session.execute(delete(db.ContinuationSnapshot).where(db.ContinuationSnapshot.id.in_(batch)))
    return {"deleted": len(batch), "remaining": len(identities) > limit}


def history_reachability(session, scope: str, *, states: list[dict]) -> tuple[set[str], set[str]]:
    """快照与可恢复检查点共同确定批次；当前工作消息的检索副本也不能抢先删。

    states必须由协调器从同一作用域仍可恢复的真实保存器读取，不能用空列表
    冒充已完成检查。此处只计算集合，不访问或改变保存器内部表。
    """
    batch_ids: set[str] = set()
    source_ids: set[str] = set()
    snapshots = session.scalars(select(db.ContinuationSnapshot).where(
        db.ContinuationSnapshot.content["task_history"]["scope"].astext == scope,
    ))
    for snapshot in snapshots:
        batch_ids.update(snapshot.content.get("task_history", {}).get("batches", []))
    for state in states:
        history = state.get("task_history", {})
        if history.get("scope") == scope:
            batch_ids.update(history.get("batches", []))
        source_ids.update(row["id"] for row in visible_records(state.get("messages", [])))
    return batch_ids, source_ids


def prune_history(session, scope: str, *, protected_batches: set[str],
                  protected_sources: set[str], limit: int = 100) -> dict:
    """先删不可达且所属Run已封闭的批次，再删没有任何存活批次引用的原文。

    派生BM25片段按既有外键CASCADE删除；源文本存在不等于Agent有权检索。
    一批最多处理limit个批次或原话，重试沿数据库真实剩余状态继续。
    """
    if not 1 <= limit <= 1000:
        raise ValueError("invalid history batch size")
    lock_control(session)
    closed_runs = select(db.Run.id).where(db.Run.lifecycle_state.in_(["cleaning", "expired"]))
    batches = list(session.scalars(select(db.TaskHistoryBatch).where(
        db.TaskHistoryBatch.scope == scope, db.TaskHistoryBatch.run_id.in_(closed_runs),
        db.TaskHistoryBatch.id.not_in(protected_batches),
    ).order_by(db.TaskHistoryBatch.id).limit(limit)))
    if batches:
        session.execute(delete(db.TaskHistoryBatch).where(
            db.TaskHistoryBatch.scope == scope, db.TaskHistoryBatch.id.in_([row.id for row in batches]),
        ))
        return {"batches": len(batches), "records": 0, "remaining": True}
    # JSONB数组包含运算在SQL中核对每条原话，避免把全scope所有正文加载到内存。
    reachable = select(db.TaskHistoryBatch.id).where(
        db.TaskHistoryBatch.scope == scope,
        db.TaskHistoryBatch.source_ids.op("?")(db.TaskHistoryRecord.source_id),
    ).correlate(db.TaskHistoryRecord).exists()
    source_ids = list(session.scalars(select(db.TaskHistoryRecord.source_id).where(
        db.TaskHistoryRecord.scope == scope, ~reachable,
        db.TaskHistoryRecord.source_id.not_in(protected_sources),
    ).order_by(db.TaskHistoryRecord.source_id).limit(limit + 1)))
    batch_ids = source_ids[:limit]
    if batch_ids:
        session.execute(delete(db.ArtifactRetentionReference).where(
            db.ArtifactRetentionReference.owner_kind == "history",
            db.ArtifactRetentionReference.owner_id.in_([f"{scope}:{identity}" for identity in batch_ids]),
        ))
        session.execute(delete(db.TaskHistoryRecord).where(
            db.TaskHistoryRecord.scope == scope, db.TaskHistoryRecord.source_id.in_(batch_ids),
        ))
    return {"batches": 0, "records": len(batch_ids), "remaining": len(source_ids) > limit}


def pin_checkpoint_artifacts(session, run_id: str, state: dict | None) -> None:
    """仍可恢复的图状态按实际内容保留材料；运行封闭后该拥有者可释放。"""
    replace_references(session, "checkpoint", run_id, state or {})
