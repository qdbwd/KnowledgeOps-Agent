"""生命周期资格与纳管时钟。查询本身无写入，删除前必须在控制锁下重查。"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import or_, select

from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.persistence import models as db

# interrupted 可能是审批、崩溃或未知结果；没有明确收束前不能以年龄猜测完成。
SETTLED = frozenset({"completed", "cancelled", "failed", "budget_exhausted"})


class LifecycleLedger(Protocol):
    def pending_root_calls(self, root_id: str) -> list[str]: ...

    def settled_root_at(self, root_id: str) -> datetime | None: ...


class LifecycleService:
    def __init__(
        self, sessions, ledger: LifecycleLedger, *,
        now: Callable[[], datetime] = db.utcnow, retention_days: int = 30,
    ):
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        self.sessions, self.ledger, self.now = sessions, ledger, now
        self.retention_days = retention_days

    def _root(self, session, root_id: str) -> db.Run:
        row = session.get(db.Run, root_id)
        if row is None or row.parent_id is not None or row.root_id != row.id:
            raise KeyError("root run not found")
        return row

    def _dependencies(self, session, root: db.Run) -> tuple[list[str], datetime | None]:
        """检查可靠交接而非表是否为空；已知完成回执不永久钉住详情。"""
        blockers: list[str] = []
        settled: list[datetime] = []
        group = list(session.scalars(select(db.Run).where(db.Run.root_id == root.id)))
        ids = [row.id for row in group]
        for row in group:
            if row.status not in SETTLED:
                blockers.append(f"run:{row.id}:{row.status}")
            elif row.finished_at is None:
                blockers.append(f"run:{row.id}:missing_finished_at")
            else:
                settled.append(row.finished_at)
        # 保守使用已完成状态白名单。未来新增中间状态会自动受到保护。
        specifications = [
            (db.ExecutionAction, db.ExecutionAction.root_id == root.id,
             {"approved", "denied", "invalidated"}, "execution_action"),
            (db.CommandExecution, db.CommandExecution.root_id == root.id,
             {"succeeded", "failed", "skipped"}, "command"),
            (db.FileOperation, db.FileOperation.root_id == root.id,
             {"succeeded", "failed", "skipped"}, "file_operation"),
            (db.RunQuestion, db.RunQuestion.root_id == root.id,
             {"answered", "stale", "invalidated", "cancelled"}, "question"),
            (db.MCPCall, db.MCPCall.run_id.in_(ids),
             {"succeeded", "failed", "denied", "invalidated"}, "mcp_call"),
            (db.MessageDelivery, or_(db.MessageDelivery.target_root_id == root.id,
                                    db.MessageDelivery.source_run_id.in_(ids)),
             {"applied", "stale"}, "message_delivery"),
            (db.ChildInstruction, db.ChildInstruction.root_id == root.id,
             {"applied", "invalidated"}, "child_instruction"),
            (db.LearningAdvance, db.LearningAdvance.run_id.in_(ids),
             SETTLED, "learning_advance"),
            (db.LearningCollaboration, db.LearningCollaboration.root_id == root.id,
             {"completed", "invalid", "failed"}, "learning_collaboration"),
        ]
        for model, predicate, complete, label in specifications:
            for row in session.scalars(select(model).where(predicate)):
                if row.status not in complete:
                    blockers.append(f"{label}:{row.id}:{row.status}")
                for name in ("updated_at", "resolved_at"):
                    stamp = getattr(row, name, None)
                    if stamp is not None:
                        settled.append(stamp)
        for row in session.scalars(select(db.ChildNotification).where(db.ChildNotification.root_id == root.id)):
            if row.consumed_at is None:
                blockers.append(f"child_notification:{row.id}:unconsumed")
            else:
                settled.append(row.consumed_at)
        for row in session.scalars(select(db.RunControl).where(db.RunControl.run_id.in_(ids))):
            if row.pause_requested:
                blockers.append(f"control:{row.run_id}:pause_requested")
        for row in session.scalars(select(db.MCPContinuation).join(db.MCPCall).where(db.MCPCall.run_id.in_(ids))):
            if row.status not in {"completed", "invalidated", "cancelled"}:
                blockers.append(f"mcp_continuation:{row.id}:{row.status}")
            if row.updated_at is not None:
                settled.append(row.updated_at)
        try:
            blockers.extend(f"model_request:{identity}:unknown" for identity in self.ledger.pending_root_calls(root.id))
            model_settled = self.ledger.settled_root_at(root.id)
            if model_settled is not None:
                settled.append(model_settled)
        except Exception:
            # 不输出可能包含 DSN 的异常文本；失败关闭，不误判为零未决请求。
            blockers.append("model_ledger_unavailable")
        return sorted(set(blockers)), max(settled) if settled else None

    def preview_root(self, root_id: str, *, ignore_age: bool = False) -> dict:
        """只读预览不创建期限或作业；会话删除仍检查依赖，只跳过30天等待。"""
        with self.sessions() as session:
            root = self._root(session, root_id)
            blockers, _ = self._dependencies(session, root)
            if root.lifecycle_state != "active":
                blockers.append(f"lifecycle:{root.lifecycle_state}")
            if not ignore_age:
                if root.details_expires_at is None:
                    blockers.append("retention_not_scheduled")
                elif root.details_expires_at > self.now():
                    blockers.append("retention_period")
            return {
                "root_id": root.id, "eligible": not blockers, "blockers": sorted(set(blockers)),
                "details_expires_at": root.details_expires_at.isoformat() if root.details_expires_at else None,
                "run_ids": list(session.scalars(select(db.Run.id).where(db.Run.root_id == root.id).order_by(db.Run.id))),
            }

    def refresh_retention(self, *, limit: int = 100, after_id: str | None = None) -> dict:
        """短事务、键集分页纳管；旧终态首次纳管给足宽限，不按旧完成日立即删除。"""
        if not 1 <= limit <= 1000:
            raise ValueError("batch limit must be between 1 and 1000")
        query = select(db.Run.id).where(db.Run.parent_id.is_(None), db.Run.lifecycle_state == "active")
        if after_id is not None:
            query = query.where(db.Run.id > after_id)
        with self.sessions() as session:
            ids = list(session.scalars(query.order_by(db.Run.id).limit(limit + 1)))
        for identity in ids[:limit]:
            with self.sessions.begin() as session:
                lock_control(session)
                root = session.scalar(select(db.Run).where(db.Run.id == identity).with_for_update())
                if root is None or root.lifecycle_state != "active":
                    continue
                stamp = self.now()
                if root.retention_adopted_at is None:
                    root.retention_adopted_at = stamp
                blockers, settled = self._dependencies(session, root)
                if blockers:
                    root.retention_eligible_at, root.details_expires_at = None, None
                else:
                    # 消费/核对晚于任务结束时，从真正解决依赖的时刻计算。
                    anchor = max(root.retention_adopted_at, settled or stamp)
                    root.retention_eligible_at = max(root.retention_eligible_at or anchor, anchor)
                    root.details_expires_at = root.retention_eligible_at + timedelta(days=self.retention_days)
        return {"processed": min(len(ids), limit), "next_cursor": ids[limit - 1] if len(ids) > limit else None}
