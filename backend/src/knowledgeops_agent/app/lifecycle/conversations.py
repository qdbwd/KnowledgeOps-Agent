"""会话删除先封闭读写，再解除来源与清理副本；用户资产有独立生命周期。"""

import hashlib
import json

from sqlalchemy import delete, func, select, update

from knowledgeops_agent.app.lifecycle.assets import (
    asset_plan,
    collect_conversation_assets,
    collect_source_files,
)
from knowledgeops_agent.app.lifecycle.jobs import job_view
from knowledgeops_agent.app.lifecycle.materials import RunRetentionCleaner
from knowledgeops_agent.app.lifecycle.provenance import scrub_source_batch
from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.harness.context.history import _digest
from knowledgeops_agent.knowledge.files import workspace_lock
from knowledgeops_agent.learning.tasks import LearningTaskService
from knowledgeops_agent.persistence import models as db

PRESERVED = ["独立保存或仍被使用的材料", "知识文档及版本", "独立保存或仍被引用的草稿", "已交付文件", "长期记忆正文", "学习目标与配置"]


def completed_deletion_view(conversation):
    """作业回收后仍可重复查询/提交删除；墓碑只证明完成，不重建删除作业。"""
    return {"id": None, "target_id": conversation.id, "status": "completed", "phase": "completed", "summary": {},
            "last_error": None, "created_at": conversation.deletion_requested_at,
            "updated_at": conversation.deleted_at, "finished_at": conversation.deleted_at}


class ConversationCleaner:
    PHASES = ("conversation_detach", "conversation_sources", "conversation_messages",
              "conversation_snapshots", "conversation_history", "conversation_runs", "conversation_assets",
              "conversation_asset_files", "conversation_finalize")

    def __init__(self, details, run_service):
        self.details, self.retained = details, RunRetentionCleaner(details)
        self.sessions, self.jobs, self.now = details.sessions, details.jobs, details.now
        self.batch_size, self.run_service = details.batch_size, run_service

    @staticmethod
    def _roots(conversation_id):
        return select(db.Run.id).where(db.Run.conversation_id == conversation_id, db.Run.parent_id.is_(None))

    def _runs(self, conversation_id):
        return select(db.Run.id).where(db.Run.root_id.in_(self._roots(conversation_id)))

    def _scopes(self, session, conversation_id):
        children = session.scalars(select(db.Run.id).where(db.Run.root_id.in_(self._roots(conversation_id)), db.Run.parent_id.is_not(None)))
        return [_digest(["conversation", conversation_id]), *(_digest(["child", child]) for child in children)]

    def _preview(self, session, conversation_id):
        row = session.get(db.Conversation, conversation_id)
        if row is None:
            raise KeyError("conversation not found")
        roots = list(session.scalars(select(db.Run).where(db.Run.id.in_(self._roots(conversation_id))).order_by(db.Run.id)))
        blockers = []
        if row.deletion_state != "active":
            blockers.append("conversation:" + row.deletion_state)
        for root in roots:
            dependencies, _ = self.details.eligibility._dependencies(session, root)
            blockers.extend(dependencies)
            if root.lifecycle_state not in {"active", "expired"}:
                blockers.append("run_cleanup_in_progress:" + root.id)
        snapshots = select(db.ContinuationSnapshot.id).where(db.ContinuationSnapshot.conversation_id == conversation_id)
        # 正常接口不会跨聊天绑定快照；遇到旧异常关系不能删除另一个聊天仍用的内容。
        if session.scalar(select(db.Run.id).where(db.Run.continuation_snapshot_id.in_(snapshots),
            db.Run.id.not_in(self._runs(conversation_id))).limit(1)):
            blockers.append("snapshot_used_by_other_conversation")
        counts = {
            "messages": session.scalar(select(func.count()).select_from(db.ChatMessage).where(db.ChatMessage.conversation_id == conversation_id)),
            "runs": session.scalar(select(func.count()).select_from(db.Run).where(db.Run.id.in_(self._runs(conversation_id)))),
            "snapshots": session.scalar(select(func.count()).select_from(db.ContinuationSnapshot).where(db.ContinuationSnapshot.conversation_id == conversation_id)),
            "history_records": session.scalar(select(func.count()).select_from(db.TaskHistoryRecord).where(db.TaskHistoryRecord.scope.in_(self._scopes(session, conversation_id)))),
            "learning_tasks": session.scalar(select(func.count()).select_from(db.LearningTask).where(db.LearningTask.conversation_id == conversation_id)),
        }
        assets = asset_plan(session, conversation_id)
        if not assets["ready"]:
            blockers.append("asset_reference_index_pending")
        tasks = list(session.execute(select(db.LearningTask.id, db.LearningTask.revision).where(db.LearningTask.conversation_id == conversation_id).order_by(db.LearningTask.id)))
        version = hashlib.sha256(json.dumps([row.id, row.deletion_state, row.user_sequence, row.runtime_revision,
            row.latest_snapshot_id, counts, assets, [tuple(task) for task in tasks],
            [(r.id, r.status, r.generation, r.event_sequence, r.lifecycle_state) for r in roots], sorted(set(blockers))], sort_keys=True).encode()).hexdigest()
        return {"conversation_id": row.id, "title": row.title if row.deletion_state == "active" else "已删除会话",
                "state": row.deletion_state, "version": version, "eligible": not blockers,
                "blockers": sorted(set(blockers)), "counts": counts, "preserved": PRESERVED,
                "assets": {key: value for key, value in assets.items() if key != "fingerprint"}}

    def preview(self, conversation_id):
        with workspace_lock(self.sessions), self.sessions.begin() as session:
            lock_control(session)
            return self._preview(session, conversation_id)

    def admit(self, conversation_id, *, expected_version):
        """预览版本只是乐观校验；实际执行仍在消息/恢复共用控制锁下重查。"""
        with workspace_lock(self.sessions), self.sessions.begin() as session:
            lock_control(session)
            conversation = session.get(db.Conversation, conversation_id)
            if conversation is None:
                raise KeyError("conversation not found")
            existing = session.scalar(select(db.CleanupJob).where(db.CleanupJob.target_kind == "conversation", db.CleanupJob.target_id == conversation_id))
            if existing is not None:
                return job_view(existing)
            if conversation.deletion_state == "deleted":
                return completed_deletion_view(conversation)
            preview = self._preview(session, conversation_id)
            if preview["version"] != expected_version:
                raise ValueError("会话状态已变化，请重新预览后删除。")
            if not preview["eligible"]:
                raise ValueError("会话仍有未完成或待核对的执行：" + ", ".join(preview["blockers"]))
            conversation.deletion_state, conversation.deletion_requested_at = "deleting", self.now()
            from knowledgeops_agent.app.conversations import touch_directory

            touch_directory(session)
            conversation.selected_learning_task_id, conversation.latest_snapshot_id = None, None
            job = self.jobs.create(session, "conversation", conversation_id)
            job.phase, job.summary = "conversation_detach", {"counts": preview["counts"], "assets": preview["assets"], "preserved": PRESERVED}
            return job_view(job)

    def retry(self, identity):
        job = self.jobs.get(identity)
        if job["target_kind"] != "conversation":
            raise KeyError("conversation deletion job not found")
        if job["cursor"].get("run_job"):
            self.jobs.retry(job["cursor"]["run_job"])
        return self.jobs.retry(identity)

    def step(self, *, identity=None):
        claim = self.jobs.claim(identity=identity, phases=self.PHASES)
        if claim is None:
            return None
        try:
            if claim["phase"] == "conversation_assets":
                indexed = self.retained.catalog.step(limit=self.batch_size)
                if not indexed["complete"]:
                    self.jobs.advance(claim["id"], claim["claim_token"], phase=claim["phase"])
                    return self.jobs.get(claim["id"])
            if claim["phase"] == "conversation_runs":
                self._runs_step(claim)
            else:
                with workspace_lock(self.sessions), self.sessions.begin() as session:
                    lock_control(session)
                    job = self.jobs.owned(session, claim["id"], claim["claim_token"])
                    conversation = session.get(db.Conversation, job.target_id)
                    if conversation is None or conversation.deletion_state != "deleting":
                        raise ValueError("conversation is not sealed for deletion")
                    if job.phase == "conversation_detach":
                        self._detach(session, conversation, job)
                    elif job.phase == "conversation_sources":
                        cursor = scrub_source_batch(session, conversation.id, job.cursor, self.batch_size)
                        self.jobs.advance_in(job, phase="conversation_sources" if cursor else "conversation_messages", cursor=cursor)
                    elif job.phase == "conversation_messages":
                        self._messages(session, conversation, job)
                    elif job.phase == "conversation_snapshots":
                        self._snapshots(session, conversation, job)
                    elif job.phase == "conversation_history":
                        self._history(session, conversation, job)
                    elif job.phase == "conversation_assets":
                        self._assets(session, conversation, job)
                    elif job.phase == "conversation_asset_files":
                        collect_source_files(session, job.cursor["originals"])
                        self.jobs.advance_in(job, phase="conversation_assets")
                    elif job.phase == "conversation_finalize":
                        self._finalize(session, conversation, job)
        except Exception as error:
            self.jobs.fail(claim["id"], claim["claim_token"], error)
        return self.jobs.get(claim["id"])

    def _detach(self, session, conversation, job):
        # 其他进程的池通过PG作用域校验回收；这里同时立即关闭本进程的连接。
        self.run_service.close_conversation_mcp(conversation.id)
        rows = list(session.scalars(select(db.LearningTask).where(db.LearningTask.conversation_id == conversation.id)
                                   .order_by(db.LearningTask.id).limit(self.batch_size + 1)))
        for row in rows[:self.batch_size]:
            row.conversation_id, row.auto_publish, row.confirmed_requirements_revision = None, False, None
            row.authority_epoch, row.revision = row.authority_epoch + 1, row.revision + 1
            if row.status not in {"completed", "partial", "cancelled"}:
                row.status, row.stop_reason = "paused", "原会话已删除；配置保留，可复用到新的学习任务。"
            LearningTaskService._record(session, row)
        self.jobs.advance_in(job, phase="conversation_detach" if len(rows) > self.batch_size else "conversation_sources")

    def _messages(self, session, conversation, job):
        rows = list(session.scalars(select(db.ChatMessage).where(db.ChatMessage.conversation_id == conversation.id)
                                   .order_by(db.ChatMessage.id).limit(self.batch_size + 1)))
        ids = [row.id for row in rows[:self.batch_size]]
        session.execute(delete(db.MessageDelivery).where(db.MessageDelivery.message_id.in_(ids)))
        session.execute(delete(db.PreferenceExtraction).where(db.PreferenceExtraction.message_id.in_(ids)))
        for row in rows[:self.batch_size]:
            session.delete(row)
        self.jobs.advance_in(job, phase="conversation_messages" if len(rows) > self.batch_size else "conversation_snapshots")

    def _snapshots(self, session, conversation, job):
        rows = list(session.scalars(select(db.ContinuationSnapshot).where(db.ContinuationSnapshot.conversation_id == conversation.id)
                                   .order_by(db.ContinuationSnapshot.id).limit(self.batch_size + 1)))
        ids = [row.id for row in rows[:self.batch_size]]
        session.execute(update(db.Run).where(db.Run.continuation_snapshot_id.in_(ids)).values(continuation_snapshot_id=None))
        session.execute(update(db.ContinuationSnapshot).where(db.ContinuationSnapshot.parent_snapshot_id.in_(ids)).values(parent_snapshot_id=None))
        for row in rows[:self.batch_size]:
            session.delete(row)
        self.jobs.advance_in(job, phase="conversation_snapshots" if len(rows) > self.batch_size else "conversation_history")

    def _history(self, session, conversation, job):
        scopes = self._scopes(session, conversation.id)
        if not job.cursor.get("batches_done"):
            rows = list(session.scalars(select(db.TaskHistoryBatch).where(db.TaskHistoryBatch.scope.in_(scopes))
                                       .order_by(db.TaskHistoryBatch.scope, db.TaskHistoryBatch.id).limit(self.batch_size + 1)))
            for row in rows[:self.batch_size]:
                session.delete(row)
            self.jobs.advance_in(job, phase="conversation_history", cursor={"batches_done": len(rows) <= self.batch_size})
            return
        rows = list(session.scalars(select(db.TaskHistoryRecord).where(db.TaskHistoryRecord.scope.in_(scopes))
                                   .order_by(db.TaskHistoryRecord.scope, db.TaskHistoryRecord.source_id).limit(self.batch_size + 1)))
        for row in rows[:self.batch_size]:
            session.delete(row)  # 派生BM25片段由已定义的外键一起删除。
        self.jobs.advance_in(job, phase="conversation_history" if len(rows) > self.batch_size else "conversation_runs",
                             cursor={"batches_done": True} if len(rows) > self.batch_size else {})

    def _runs_step(self, claim):
        """复用原Run作业及其故障恢复；不在父事务里嵌套取得同一PG控制锁。"""
        cursor = claim["cursor"]
        if not cursor.get("run_job"):
            query = self._roots(claim["target_id"])
            if cursor.get("after"):
                query = query.where(db.Run.id > cursor["after"])
            with self.sessions() as session:
                root_id = session.scalar(query.order_by(db.Run.id).limit(1))
            if root_id is None:
                self.jobs.advance(claim["id"], claim["claim_token"], phase="conversation_assets")
                return
            child = self.details.admit(root_id, deleting_conversation=claim["target_id"])
            self.jobs.advance(claim["id"], claim["claim_token"], phase="conversation_runs", cursor={"root": root_id, "run_job": child["id"]})
            return
        child = self.jobs.get(cursor["run_job"])
        if child["status"] != "completed":
            component = self.details if child["phase"] in self.details.PHASES else self.retained
            component.step(identity=child["id"])
            child = self.jobs.get(child["id"])
        if child["status"] == "failed":
            raise ValueError("dependent run cleanup needs retry")
        self.jobs.advance(claim["id"], claim["claim_token"], phase="conversation_runs",
                          cursor={"after": cursor["root"]} if child["status"] == "completed" else cursor)

    def _assets(self, session, conversation, job):
        result = collect_conversation_assets(session, conversation.id, limit=self.batch_size)
        summary = dict(job.summary)
        for key in ("drafts_deleted", "versions_deleted"):
            summary[key] = summary.get(key, 0) + result[key]
        phase = "conversation_asset_files" if result.get("originals") else "conversation_assets" if result["more"] else "conversation_finalize"
        self.jobs.advance_in(job, phase=phase, summary=summary,
                             cursor={"originals": result["originals"]} if result.get("originals") else {})

    def _finalize(self, session, conversation, job):
        query = select(db.Run).where(db.Run.id.in_(self._runs(conversation.id)))
        if job.cursor.get("after"):
            query = query.where(db.Run.id > job.cursor["after"])
        rows = list(session.scalars(query.order_by(db.Run.id).limit(self.batch_size + 1)))
        for row in rows[:self.batch_size]:
            if row.lifecycle_state not in {"expired", "deleted"}:
                raise ValueError("run cleanup has not completed")
            row.task, row.input_data, row.outcome, row.details_summary = "", {}, {}, {}
            row.stop_reason, row.lifecycle_state = None, "deleted"
        more = len(rows) > self.batch_size
        if not more:
            session.execute(delete(db.ExecutionRule).where(db.ExecutionRule.conversation_id == conversation.id))
            conversation.title, conversation.runtime_settings = "已删除会话", {}
            conversation.deletion_state, conversation.deleted_at = "deleted", self.now()
        self.jobs.advance_in(job, phase="conversation_finalize" if more else "completed",
                             cursor={"after": rows[self.batch_size - 1].id} if more else {}, completed=not more)
