"""清理协调器的执行详情阶段：先封闭执行，再清理可恢复详情与缓存。

此阶段完成后交接到 retained_data；它不自行宣称材料/会话清理已经完成。
跨库或缓存失败由同一持久作业续跑，所有外部操作都允许重复。
"""

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext

from sqlalchemy import delete, select, tuple_

from knowledgeops_agent.app.lifecycle.continuity import plan_protocol_backfill
from knowledgeops_agent.app.lifecycle.idempotency import freeze_request
from knowledgeops_agent.app.lifecycle.jobs import CleanupJobs, job_view
from knowledgeops_agent.app.lifecycle.service import LifecycleService
from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.persistence import models as db


class RunDetailCleaner:
    """一次步骤有界执行；删除已成功但提交前崩溃时，重放不会重做业务。"""

    PHASES = ("seal", "checkpoints", "requests", "runtime_records", "caches")

    def __init__(
        self, sessions, ledger, saver: Callable[[], AbstractContextManager], *,
        now=db.utcnow, batch_size: int = 100, lease_seconds: int = 60,
        retry_seconds: int = 60, retention_days: int = 30,
        event_bridge=None, checkpoint_cache=None, usage_reader=None,
    ):
        if not 1 <= batch_size <= 1000:
            raise ValueError("batch size must be between 1 and 1000")
        self.sessions, self.ledger, self.saver = sessions, ledger, saver
        self.now, self.batch_size = now, batch_size
        self.event_bridge, self.checkpoint_cache = event_bridge, checkpoint_cache
        self.usage_reader = usage_reader
        self.jobs = CleanupJobs(sessions, now=now, lease_seconds=lease_seconds, retry_seconds=retry_seconds)
        self.eligibility = LifecycleService(sessions, ledger, now=now, retention_days=retention_days)

    def _plan(self, session, root: db.Run, *, ignore_age: bool = False, preserve_protocol: bool = True) -> tuple[dict, dict]:
        blockers, _ = self.eligibility._dependencies(session, root)
        if root.lifecycle_state != "active":
            blockers.append(f"lifecycle:{root.lifecycle_state}")
        if not ignore_age:
            if root.details_expires_at is None:
                blockers.append("retention_not_scheduled")
            elif root.details_expires_at > self.now():
                blockers.append("retention_period")
        group = list(session.scalars(select(db.Run).where(db.Run.root_id == root.id).order_by(db.Run.id)))
        updates = {}
        if not blockers and preserve_protocol:
            try:
                records = self.ledger.records(root_id=root.id)
                for run in group:
                    changes, reasons = plan_protocol_backfill(session, run, records)
                    updates.update(changes)
                    blockers.extend(reasons)
            except Exception:
                blockers.append("conversation_protocol_unavailable")
        return {
            "root_id": root.id, "eligible": not blockers, "blockers": sorted(set(blockers)),
            "run_ids": [run.id for run in group],
            "details_expires_at": root.details_expires_at.isoformat() if root.details_expires_at else None,
            "protocol_backfill_count": len(updates),
        }, updates

    def preview(self, root_id: str) -> dict:
        with self.sessions() as session:
            root = self.eligibility._root(session, root_id)
            return self._plan(session, root)[0]

    def _removal_plan(self, session, root):
        if root.lifecycle_state == "active":
            preview, updates = self._plan(session, root, ignore_age=True)
        else:
            blockers, _ = self.eligibility._dependencies(session, root)
            preview, updates = {"blockers": blockers}, {}
        group = list(session.scalars(select(db.Run).where(db.Run.root_id == root.id).order_by(db.Run.id)))
        blockers = list(preview["blockers"])
        conversation = session.get(db.Conversation, root.conversation_id) if root.conversation_id else None
        if conversation and conversation.deletion_state != "active":
            blockers.append("conversation_not_active")
        state = [(r.id, r.status, r.generation, r.lifecycle_state, str(r.user_deleted_at), str(r.finished_at)) for r in group]
        fingerprint = hashlib.sha256(json.dumps([state, sorted(blockers), sorted(updates)], sort_keys=True).encode()).hexdigest()
        return {"version": fingerprint, "eligible": not blockers, "blockers": sorted(set(blockers)),
            "title": root.task[:300], "counts": {"delete": len(group), "keep": 0},
            "reasons": [f"将移除这组 {len(group)} 条运行记录，并清理模型请求详情、工具活动、检查点和无依赖的内部临时副本。",
                "聊天消息及接续信息、已发布文章、仍被引用的内容、独立材料和交付文件保留。",
                "仅能删除已结束且没有待处理操作的任务；删除后不能从原执行位置恢复。"]}, updates

    def removal_preview(self, root_id):
        with self.sessions.begin() as session:
            lock_control(session)
            return self._removal_plan(session, self.eligibility._root(session, root_id))[0]

    def remove(self, root_id, *, expected_version):
        return self.admit(root_id, removal_version=expected_version)

    def admit(self, root_id: str, *, deleting_conversation: str | None = None, removal_version: str | None = None) -> dict:
        """与消息/恢复共享控制锁，原子提交协议和封闭状态，之后才允许删除。

        这里只保存程序可证明的消息协议；旧记录不能转换时阻止清理。
        root已封闭时返回原作业，不建立新预算或重新开放运行。
        """
        with self.sessions.begin() as session:
            lock_control(session)
            root = self.eligibility._root(session, root_id)
            if deleting_conversation is not None:
                conversation = session.get(db.Conversation, deleting_conversation)
                if (conversation is None or conversation.deletion_state != "deleting"
                        or root.conversation_id != deleting_conversation):
                    raise ValueError("conversation deletion has not sealed this root")
            existing = session.scalar(select(db.CleanupJob).where(
                db.CleanupJob.target_kind == "run", db.CleanupJob.target_id == root.id,
            ))
            removal, removal_updates = None, {}
            if removal_version is not None:
                if root.user_deleted_at and existing:
                    return job_view(existing)
                removal, removal_updates = self._removal_plan(session, root)
                if removal["version"] != removal_version:
                    raise ValueError("运行状态已变化，请重新核对删除范围。")
                if not removal["eligible"]:
                    raise ValueError("运行仍有执行或接续依赖，暂不能删除：" + ", ".join(removal["blockers"]))
                for row in session.scalars(select(db.Run).where(db.Run.root_id == root.id)):
                    row.user_deleted_at = self.now()
                if existing:
                    existing.summary = {**existing.summary, "title": removal["title"], "user_requested": True}
                elif root.lifecycle_state == "expired":
                    job = self.jobs.create(session, "run", root.id)
                    root.lifecycle_state = "cleaning"
                    self.jobs.advance_in(job, phase="retained_data", summary={"title": removal["title"],
                        "user_requested": True, "run_count": removal["counts"]["delete"]})
                    return job_view(job)
            # 聊天引用已经解除时，旧详情到期作业也需再走材料阶段。不能重新打开执行。
            if deleting_conversation is not None and root.lifecycle_state == "expired":
                job = existing or self.jobs.create(session, "run", root.id)
                root.lifecycle_state = "cleaning"
                self.jobs.advance_in(job, phase="retained_data")
                return job_view(job)
            if existing is not None:
                return job_view(existing)
            preview, updates = (removal, removal_updates) if removal is not None else self._plan(
                session, root, ignore_age=deleting_conversation is not None, preserve_protocol=deleting_conversation is None)
            if not preview["eligible"]:
                raise ValueError("cleanup is protected: " + ", ".join(preview["blockers"]))
            usage = self.usage_reader(root.id) if self.usage_reader and root.conversation_id and deleting_conversation is None else None
            for identity, payload in updates.items():
                session.get(db.ChatMessage, identity).protocol_payload = payload
            group = list(session.scalars(select(db.Run).where(db.Run.root_id == root.id)))
            for row in group:
                row.details_summary = {
                    "status": row.status, "kind": row.kind,
                    "answer": str(row.outcome.get("answer", ""))[:2000],
                    "draft_ids": row.outcome.get("draft_ids", []),
                    "published_ids": row.outcome.get("published_ids", []),
                    "stop_reason": (row.stop_reason or "")[:500],
                    "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                    "business_outcome": {key: row.outcome[key] for key in (
                        "kind", "source_id", "source_version_id", "delivery_id", "target_root_id", "target_generation", "answer_origin",
                    ) if key in row.outcome},
                }
                if row.outcome.get("answer_origin") == "application_receipt":
                    row.details_summary["business_outcome"]["application_receipt_sha256"] = hashlib.sha256(
                        str(row.outcome.get("answer", "")).encode(),
                    ).hexdigest()
                freeze_request(row)
                row.lifecycle_state = "cleaning"
            if usage:
                usage = dict(usage)
                if hasattr(usage.get("measured_at"), "isoformat"):
                    usage["measured_at"] = usage["measured_at"].isoformat()
                root.details_summary = {**root.details_summary, "context_usage": usage}
            job = self.jobs.create(session, "run", root.id)
            job.phase, job.summary = "seal", {"run_count": len(group)}
            if removal:
                job.summary = {**job.summary, "title": removal["title"], "user_requested": True}
            return job_view(job)

    def step(self, *, identity: str | None = None) -> dict | None:
        """只领取本组件拥有的阶段，结束后保留作业供材料/引用阶段接续。"""
        claim = self.jobs.claim(identity=identity, phases=self.PHASES)
        if claim is None:
            return None
        try:
            # 保存器setup可能创建并发索引；必须在业务事务开始前完成，避免等待自己。
            with (self.saver() if claim["phase"] == "checkpoints" else nullcontext()) as saver, self.sessions.begin() as session:
                lock_control(session)
                job = self.jobs.owned(session, claim["id"], claim["claim_token"])
                root = self.eligibility._root(session, job.target_id)
                if root.lifecycle_state != "cleaning":
                    raise ValueError("cleanup root is not sealed for detail removal")
                if job.phase == "seal":
                    self.ledger.seal_root(root.id)
                    self.jobs.advance_in(job, phase="checkpoints")
                elif job.phase == "checkpoints":
                    assert saver is not None
                    rows, more = self._run_batch(session, root.id, job.cursor)
                    for row in rows:
                        saver.delete_thread(row.checkpoint_id)
                    self.jobs.advance_in(job, phase="checkpoints" if more else "requests",
                                         cursor={"after": rows[-1].id} if more else {})
                elif job.phase == "requests":
                    result = self.ledger.purge_root_details(root.id, limit=self.batch_size)
                    self.jobs.advance_in(job, phase="requests" if result["remaining"] else "runtime_records")
                elif job.phase == "runtime_records":
                    self._runtime_step(session, root, job)
                elif job.phase == "caches":
                    rows, more = self._run_batch(session, root.id, job.cursor)
                    for row in rows:
                        if self.checkpoint_cache is not None:
                            self.checkpoint_cache.delete_thread(row.checkpoint_id, strict=True)
                        if self.event_bridge is not None:
                            self.event_bridge.delete_run(row.id)
                    self.jobs.advance_in(job, phase="caches" if more else "retained_data",
                                         cursor={"after": rows[-1].id} if more else {})
        except Exception as error:
            self.jobs.fail(claim["id"], claim["claim_token"], error)
        return self.jobs.get(claim["id"])

    def _run_batch(self, session, root_id: str, cursor: dict):
        query = select(db.Run).where(db.Run.root_id == root_id)
        if cursor.get("after"):
            query = query.where(db.Run.id > cursor["after"])
        rows = list(session.scalars(query.order_by(db.Run.id).limit(self.batch_size + 1)))
        return rows[:self.batch_size], len(rows) > self.batch_size

    def _runtime_step(self, session, root: db.Run, job: db.CleanupJob) -> None:
        """只删完整运行详情；文件操作和材料另阶段核对文件/引用后处理。

        先删MCP续接再删问答/调用，保持外键。保留草稿、发布及独立业务回执。
        一步至多一张表的一批记录，游标由表名决定，不依赖已删除行的存在。
        """
        run_ids = select(db.Run.id).where(db.Run.root_id == root.id)
        call_ids = select(db.MCPCall.id).where(db.MCPCall.run_id.in_(run_ids))
        specifications = [
            (db.MCPContinuation, db.MCPContinuation.mcp_call_id.in_(call_ids)),
            (db.MCPCall, db.MCPCall.run_id.in_(run_ids)),
            (db.RunQuestion, db.RunQuestion.run_id.in_(run_ids)),
            (db.ExecutionAction, db.ExecutionAction.root_id == root.id),
            (db.CommandExecution, db.CommandExecution.root_id == root.id),
            (db.ModelStep, db.ModelStep.run_id.in_(run_ids)),
            (db.ChildInstruction, db.ChildInstruction.root_id == root.id),
            (db.ChildNotification, db.ChildNotification.root_id == root.id),
            (db.ToolBudgetReservation, db.ToolBudgetReservation.run_id.in_(run_ids)),
            (db.RunControl, db.RunControl.run_id.in_(run_ids)),
            (db.RunDispatch, db.RunDispatch.run_id.in_(run_ids)),
            (db.RunResourceSnapshot, db.RunResourceSnapshot.root_id == root.id),
            (db.RunEvent, db.RunEvent.run_id.in_(run_ids)),
            # 追加到末尾，保持已经持久化的旧表序号游标含义不变。
            (db.RunSkillUse, db.RunSkillUse.run_id.in_(run_ids)),
        ]
        index = int(job.cursor.get("table", 0))
        model, predicate = specifications[index]
        columns = list(model.__mapper__.primary_key)
        keys = list(session.execute(select(*columns).where(predicate).order_by(*columns).limit(self.batch_size + 1)))
        if keys:
            session.execute(delete(model).where(tuple_(*columns).in_([tuple(key) for key in keys[:self.batch_size]])))
        if len(keys) <= self.batch_size:
            index += 1
        self.jobs.advance_in(job, phase="runtime_records" if index < len(specifications) else "caches",
                             cursor={"table": index} if index < len(specifications) else {})
