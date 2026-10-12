"""PostgreSQL 持久任务记录与运行控制，投递由 Celery/Redis 完成。

API 只入队和读取，worker 在进程级租约保护下领取任务。状态、事件和用户
消息使用同一数据库事务；程序重启不会自动重做收费操作。模型请求额度另由
ModelLedger 执行，这里负责工具/搜索/活跃时间及取消信号。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, Field
from sqlalchemy import Table, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from knowledgeops_agent.db import (
    ChangeSet,
    ChatMessage,
    ContinuationSnapshot,
    Conversation,
    Run,
    RunArtifact,
    RunDispatch,
    RunEvent,
    TaskGrant,
    new_id,
    utcnow,
)
from knowledgeops_agent.pagination import page_rows

if TYPE_CHECKING:
    from knowledgeops_agent.event_stream import RedisEventBridge

TERMINAL = {"completed", "cancelled", "failed", "budget_exhausted", "interrupted"}


class RunStopped(RuntimeError):
    """确定性控制信号；worker 保存部分结果，不让模型自行撤销停止。"""


class SteeringPending(RunStopped):
    """新引导尚未在模型安全点应用，所有新的业务写入先暂停。"""


class Budgets(BaseModel):
    model_requests: int = Field(default=40, ge=1, le=600)
    tool_calls: int = Field(default=80, ge=1, le=2000)
    searches: int = Field(default=20, ge=0, le=200)
    active_seconds: int = Field(default=1200, ge=1, le=7200)


class RunService:
    """持有连接工厂，单次方法持有短事务，不把数据库锁跨越模型调用。"""

    def __init__(
        self,
        sessions,
        *,
        now: Callable[[], datetime] = utcnow,
        event_bridge: RedisEventBridge | None = None,
    ):
        self.sessions = sessions
        self.now = now
        self.event_bridge = event_bridge
        self.live_check: Callable[[], None] | None = None

    def _assert_live(self) -> None:
        if self.live_check:
            try:
                self.live_check()
            except RuntimeError:
                raise RunStopped("worker lease lost") from None

    def _locked(self, session, run_id: str) -> Run:
        from knowledgeops_agent.run_control import lock_control

        lock_control(session)
        # 同一 Session 可能已经无锁读取过这行。拿到锁后必须刷新 identity map，
        # 否则 SQLAlchemy 仍会返回旧计数/序号，覆盖并发工具刚提交的更新。
        run = session.scalar(
            select(Run)
            .where(Run.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if run is None:
            raise KeyError("run not found")
        return run

    def _event(self, session, run: Run, kind: str, data: dict) -> None:
        # 调用者已持有运行行锁；序号分配和事件插入不可分离。
        run.event_sequence += 1
        row = RunEvent(
            run_id=run.id,
            sequence=run.event_sequence,
            kind=kind,
            data=data,
            created_at=self.now(),
        )
        session.add(row)
        if self.event_bridge:
            self.event_bridge.stage(session, row)

    def _active_ms(self, run: Run, *, until: datetime | None = None) -> int:
        elapsed = (
            0
            if run.active_since is None
            else max(0, int(((until or self.now()) - run.active_since).total_seconds() * 1000))
        )
        return run.active_ms + elapsed

    def _pause_clock(self, run: Run, *, until: datetime | None = None) -> None:
        run.active_ms = self._active_ms(run, until=until)
        run.active_since = None

    def _view(self, run: Run) -> dict:
        return {
            "id": run.id,
            "root_id": run.root_id,
            "parent_id": run.parent_id,
            "conversation_id": run.conversation_id,
            "learning_task_id": run.learning_task_id,
            "learning_unit_id": run.learning_unit_id,
            "origin": run.origin,
            "checkpoint_id": run.checkpoint_id,
            "generation": run.generation,
            "format_version": run.format_version,
            "conversation_sequence": run.conversation_sequence,
            "execution_sequence": run.execution_sequence,
            "continuation_snapshot_id": run.continuation_snapshot_id,
            "authority_epoch": run.authority_epoch,
            "applied_authority_epoch": run.applied_authority_epoch,
            "kind": run.kind,
            "task": run.task,
            "input": run.input_data,
            "budgets": run.budgets,
            "status": run.status,
            "outcome": run.outcome,
            "stop_reason": run.stop_reason,
            "cancel_requested": run.cancel_requested,
            "tool_calls": run.tool_calls,
            "searches": run.searches,
            "active_ms": self._active_ms(run),
            "created_at": run.created_at.isoformat(),
            "last_event": run.event_sequence,
        }

    def create_conversation(self, title: str = "新对话") -> dict:
        with self.sessions.begin() as session:
            row = Conversation(title=title.strip()[:300] or "新对话", created_at=self.now())
            session.add(row)
            session.flush()
            return {"id": row.id, "title": row.title}

    def conversations(self) -> list[dict]:
        with self.sessions() as session:
            return [
                {
                    "id": row.id,
                    "title": row.title,
                    "selected_learning_task_id": row.selected_learning_task_id,
                }
                for row in session.scalars(
                    select(Conversation).order_by(Conversation.created_at.desc(), Conversation.id)
                )
            ]

    def messages(self, conversation_id: str) -> list[dict]:
        with self.sessions() as session:
            if session.get(Conversation, conversation_id) is None:
                raise KeyError("conversation not found")
            # 自动运行没有真人序号，按领取时的执行顺序排放它的回复。尚未执行
            # 的真人消息显示在队尾；即时引导属于它实际投递的运行，不伪造执行序号。
            from knowledgeops_agent.db import MessageDelivery

            target = aliased(Run)
            rows = session.scalars(
                select(ChatMessage)
                .join(Run, Run.id == ChatMessage.run_id)
                .outerjoin(MessageDelivery, MessageDelivery.message_id == ChatMessage.id)
                .outerjoin(target, target.id == MessageDelivery.target_root_id)
                .where(ChatMessage.conversation_id == conversation_id)
                .order_by(
                    func.coalesce(Run.execution_sequence, target.execution_sequence, 2147483647),
                    ChatMessage.user_sequence,
                    ChatMessage.role.desc(),
                    ChatMessage.created_at,
                    ChatMessage.id,
                )
            )
            return [
                {
                    "id": row.id,
                    "run_id": row.run_id,
                    "role": row.role,
                    "content": row.content,
                    "user_sequence": row.user_sequence,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    def messages_for_run(self, run_id: str) -> list[dict]:
        """为模型和对话快照组织截至本任务的完整轮次。

        用户可在 A 执行时提交 B。A 的答案会晚于 B 的用户消息落库，不能
        按该消息的时间截断。先按用户消息确定轮次，再带上先前轮次已经
        保存的回复；当前轮次只包含用户任务，未来轮次和子研究均不进入。
        原消息与时间戳不修改，图恢复仍以自己的检查点为准。
        """
        run = self.get_run(run_id)
        if run["parent_id"] or not run["conversation_id"]:
            raise ValueError("conversation context requires a root conversation task")
        rows = self.messages(run["conversation_id"])
        from knowledgeops_agent.guidance import applied_messages

        guidance_ids = {item["id"] for item in applied_messages(self, run_id)}
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            grouped.setdefault(row["run_id"], []).append(row)
        result: list[dict] = []
        for row in rows:
            if row["role"] != "user":
                continue
            if row["run_id"] == run_id:
                return [*result, row, *[item for item in rows if item["id"] in guidance_ids]]
            result.extend(grouped[row["run_id"]])
        raise ValueError("task has no user message in this conversation")

    def enqueue(
        self,
        conversation_id: str,
        task: str,
        *,
        request_key: str,
        publication_mode: Literal["ask", "preview", "direct"] = "ask",
        knowledge_only: bool = False,
        delegation: bool = True,
        auxiliary: bool = False,
        source_ids: list[str] | None = None,
        budgets: dict | None = None,
        learning_task_id: str | None = None,
    ) -> dict:
        """可信用户入口原子保存消息和任务；重复请求必须具有相同内容与选项。"""
        if not task.strip() or len(task) > 100000 or not 1 <= len(request_key) <= 200:
            raise ValueError("nonempty task and valid request key are required")
        if publication_mode not in {"ask", "preview", "direct"}:
            raise ValueError("invalid publication mode")
        limits = Budgets.model_validate(budgets or {}).model_dump()
        inputs: dict = {
            "publication_mode": publication_mode,
            "knowledge_only": knowledge_only,
            "delegation": delegation,
            "auxiliary": auxiliary,
            "source_ids": source_ids or [],
        }
        if learning_task_id is not None:
            inputs["learning_task_id"] = learning_task_id
        with self.sessions.begin() as session:
            # 同会话重试串行化；跨会话相同 key 被唯一约束拒绝。
            conversation = session.scalar(
                select(Conversation).where(Conversation.id == conversation_id).with_for_update()
            )
            if conversation is None:
                raise KeyError("conversation not found")
            prior = session.scalar(select(Run).where(Run.request_key == request_key))
            if prior:
                # 旧任务缺少辅助开关等价于关闭；仅规范化比较，不回写历史输入。
                prior_inputs = {"auxiliary": False, **prior.input_data}
                if (prior.conversation_id, prior.task, prior_inputs, prior.budgets) != (
                    conversation_id,
                    task,
                    inputs,
                    limits,
                ):
                    raise ValueError("idempotency key belongs to a different request")
                return self._view(prior)
            selected_task_id = learning_task_id or conversation.selected_learning_task_id
            if selected_task_id:
                from knowledgeops_agent.db import LearningTask

                selected_task = session.get(LearningTask, selected_task_id)
                if selected_task is None or selected_task.conversation_id != conversation_id:
                    raise ValueError("学习任务不属于当前会话")
            run_id = new_id()
            conversation.user_sequence += 1
            run = Run(
                id=run_id,
                root_id=run_id,
                checkpoint_id=run_id,
                conversation_id=conversation_id,
                learning_task_id=selected_task_id,
                conversation_sequence=conversation.user_sequence,
                request_key=request_key,
                task=task,
                input_data=inputs,
                budgets=limits,
                created_at=self.now(),
            )
            session.add(run)
            session.flush()
            session.add(
                RunDispatch(run_id=run.id, generation=run.generation, next_attempt_at=self.now())
            )
            message = ChatMessage(
                conversation_id=conversation_id,
                run_id=run_id,
                role="user",
                content=task,
                user_sequence=conversation.user_sequence,
                created_at=self.now(),
            )
            session.add(message)
            session.flush()
            if publication_mode == "direct":
                session.add(TaskGrant(root_id=run_id, mode="direct", actor="user"))
            # 用户提交时冻结已展示草稿的版本；排队期间的编辑不能被旧确认覆盖。
            candidates = []
            seen_drafts: set[str] = set()
            for previous in session.scalars(
                select(Run)
                .where(
                    Run.conversation_id == conversation_id,
                    Run.id != run_id,
                    Run.parent_id.is_(None),
                    Run.status == "completed",
                )
                .order_by(Run.conversation_sequence.desc(), Run.created_at.desc(), Run.id)
                .limit(100)
            ):
                shown = previous.outcome.get("draft_ids", [])
                for draft in session.scalars(
                    select(ChangeSet)
                    .join(Run, Run.id == ChangeSet.root_id)
                    .where(
                        Run.conversation_id == conversation_id,
                        ChangeSet.id.in_(shown),
                        ChangeSet.status.in_(["draft", "approved"]),
                    )
                ):
                    if draft.id in seen_drafts:
                        continue
                    seen_drafts.add(draft.id)
                    candidates.append(
                        {
                            "id": draft.id,
                            "root_id": draft.root_id,
                            "revision": previous.outcome.get("draft_revisions", {}).get(draft.id),
                            "requires_impact_confirmation": draft.requires_impact_confirmation,
                            "operations": [
                                {
                                    key: item.get(key)
                                    for key in ("action", "document_id", "base_version_id", "title")
                                }
                                for item in draft.operations
                            ],
                        }
                    )
            self._event(
                session,
                run,
                "queued",
                {
                    "status": "queued",
                    "publication_context": {
                        "message": {"id": message.id, "content": message.content},
                        "candidates": candidates[:20],
                        "candidates_truncated": len(candidates) > 20,
                        "known_preferences": self._preference_context(session, task),
                        "learning_context": self._learning_context(
                            session, conversation_id, selected_task_id
                        ),
                    },
                },
            )
            return self._view(run)

    @staticmethod
    def _learning_context(session, conversation_id: str, selected_task_id: str | None) -> dict:
        from knowledgeops_agent.learning_intent import intent_context

        return intent_context(session, conversation_id, selected_task_id)

    @staticmethod
    def _preference_context(session, query: str | None = None) -> list[dict]:
        from knowledgeops_agent.preferences import known_preferences

        return known_preferences(session, query)

    def publication_context(self, run_id: str) -> dict:
        """仅使用可信消息表和提交时的草稿快照，不从 Agent State/摘要恢复授权。"""
        with self.sessions() as session:
            run = session.get(Run, run_id)
            if run is None or run.parent_id or run.kind != "agent" or run.origin != "human":
                raise PermissionError("publication intent requires a root user task")
            message = session.scalar(
                select(ChatMessage).where(
                    ChatMessage.run_id == run_id,
                    ChatMessage.role == "user",
                    ChatMessage.conversation_id == run.conversation_id,
                )
            )
            if message is None or message.content != run.task:
                raise PermissionError("authoritative user message is missing")
            queued = session.scalar(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.kind == "queued")
                .order_by(RunEvent.sequence)
            )
            snapshot = queued.data.get("publication_context", {}) if queued else {}
            if snapshot.get("message", {}).get("id") != message.id:
                # 旧运行没有提交快照时只支持本轮任务选择，绝不猜历史草稿版本。
                return {
                    "message": {"id": message.id, "content": message.content},
                    "candidates": [],
                    "candidates_truncated": False,
                }
            return snapshot

    def publication_intent(self, run_id: str) -> dict | None:
        """按事件种类读取，长运行超过分页上限也能恢复同一个意图结果。"""
        with self.sessions() as session:
            event = session.scalar(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.kind == "publication_intent")
                .order_by(RunEvent.sequence.desc())
                .limit(1)
            )
            return event.data if event else None

    def save_publication_intent(self, run_id: str, worker_id: str, result: dict) -> dict:
        """决策记录和本轮直接保存授权原子写入；重放只读旧决定。"""
        with self.sessions.begin() as session:
            run = self._active_root_guard(session, run_id, worker_id)
            prior = session.scalar(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.kind == "publication_intent")
                .order_by(RunEvent.sequence.desc())
                .limit(1)
            )
            if prior:
                return prior.data
            result = {**result, "authority_epoch": 0}
            if result["mode"] == "direct" and run.authority_epoch == 0:
                session.execute(
                    insert(TaskGrant)
                    .values(root_id=run.id, mode="direct", actor="user")
                    .on_conflict_do_nothing()
                )
            else:
                session.execute(delete(TaskGrant).where(TaskGrant.root_id == run.id))
            self._event(session, run, "publication_intent", result)
            return result

    def suspend_direct_grant(self, run_id: str, worker_id: str) -> None:
        """解释UI direct与当前消息是否冲突前暂挂许可，模型失败也不能留下矛盾授权。"""
        with self.sessions.begin() as session:
            self._active_root_guard(session, run_id, worker_id)
            session.execute(delete(TaskGrant).where(TaskGrant.root_id == run_id))

    def save_publication_result(self, run_id: str, worker_id: str, result: dict) -> dict:
        """保存跨轮发布回执，模型输出无法伪造它；恢复可通过发布幂等回执补写。"""
        with self.sessions.begin() as session:
            run = self._active_root_guard(session, run_id, worker_id)
            if result.get("authority_epoch", 0) < run.applied_authority_epoch:
                return result  # 新引导已应用，旧发布回执不能覆盖其授权决定。
            self._event(session, run, "publication_intent", result)
            return result

    def get_run(self, run_id: str) -> dict:
        with self.sessions() as session:
            row = session.get(Run, run_id)
            if row is None:
                raise KeyError("run not found")
            return self._view(row)

    def enqueue_material(self, inputs: dict, *, request_key: str) -> dict:
        """材料解析走同一持久任务与 outbox，但不创建对话、不调用模型。

        以请求键串行化重复提交；恢复/取消沿用相同状态机。PDF 的原件版本或
        公共网页 URL 由上传接口提供，不能把任意文件路径交给 worker。
        """
        if inputs.get("type") not in {"web", "pdf"} or not 1 <= len(request_key) <= 160:
            raise ValueError("invalid material request")
        key = "material:" + request_key
        with self.sessions.begin() as session:
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
            )
            prior = session.scalar(select(Run).where(Run.request_key == key))
            if prior:
                if prior.kind != "material" or prior.input_data != inputs:
                    raise ValueError("idempotency key belongs to a different material")
                return self._view(prior)
            run_id = new_id()
            run = Run(
                id=run_id,
                root_id=run_id,
                checkpoint_id=run_id,
                request_key=key,
                kind="material",
                origin="system",
                task="导入网页材料" if inputs["type"] == "web" else "解析 PDF 材料",
                input_data=inputs,
                budgets=Budgets(model_requests=1, tool_calls=1, searches=0).model_dump(),
                created_at=self.now(),
            )
            session.add(run)
            session.flush()
            session.add(
                RunDispatch(run_id=run.id, generation=run.generation, next_attempt_at=self.now())
            )
            self._event(session, run, "queued", {"status": "queued", "kind": "material"})
            return self._view(run)

    def related_changesets(self, run_id: str) -> list[dict]:
        """每轮刷新业务事实，避免模型把旧聊天中的待确认状态当成当前状态。

        只返回当前会话、截至本任务的最近变更集元数据；正文按需读取，不把
        所有历史草稿反复塞入提示词。此视图提供事实，不授予发布权限。
        """
        with self.sessions() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise KeyError("run not found")
            query = select(ChangeSet).join(Run, Run.id == ChangeSet.root_id)
            query = query.where(
                Run.conversation_id == run.conversation_id,
                Run.created_at <= run.created_at,
            )
            return [
                {"id": row.id, "status": row.status, "revision": row.revision}
                for row in session.scalars(query.order_by(ChangeSet.created_at.desc()).limit(20))
            ]

    def list_runs(
        self, *, conversation_id: str | None = None, root_id: str | None = None
    ) -> list[dict]:
        return self.runs_page(conversation_id=conversation_id, root_id=root_id, limit=100)["items"]

    def runs_page(
        self,
        *,
        conversation_id: str | None = None,
        root_id: str | None = None,
        status: str | None = None,
        roots_only: bool = False,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict:
        """运行列表提供独立的root/状态过滤；当前活动任务不能从最近N条猜测。"""
        query = select(Run)
        if conversation_id:
            query = query.where(Run.conversation_id == conversation_id)
        if root_id:
            query = query.where(Run.root_id == root_id)
        if roots_only:
            query = query.where(Run.parent_id.is_(None))
        if status:
            query = query.where(Run.status == status)
        with self.sessions() as session:
            rows, next_cursor = page_rows(session, query, Run, cursor=cursor, limit=limit)
            return {"items": [self._view(run) for run in rows], "next_cursor": next_cursor}

    def claim(
        self, worker_id: str, *, run_id: str | None = None, generation: int | None = None
    ) -> dict | None:
        """Celery 消费后原子领取指定运行；重复/旧代次消息不能再次执行。

        全局串行领取锁保证首版同时只有一个活跃根任务。未传 run_id 的
        选择方式保留给服务层合同测试；生产由消息指定 ID，不轮询领取。
        """
        self._assert_live()
        with self.sessions.begin() as session:
            session.execute(text("SELECT pg_advisory_xact_lock(748616002)"))
            if session.scalar(
                select(Run.id).where(Run.parent_id.is_(None), Run.status == "running").limit(1)
            ):
                return None
            query = select(Run).where(Run.status == "queued", Run.parent_id.is_(None))
            if run_id is not None:
                if generation is None:
                    raise ValueError("delivery generation is required")
                query = query.where(Run.id == run_id, Run.generation == generation)
            run = session.scalar(
                query.order_by(Run.created_at, Run.id).with_for_update(skip_locked=True).limit(1)
            )
            if run is None:
                return None
            if run.conversation_id:
                earlier = session.scalar(
                    select(Run.id)
                    .where(
                        Run.conversation_id == run.conversation_id,
                        Run.parent_id.is_(None),
                        Run.status == "queued",
                        Run.origin == "human",
                        Run.id != run.id,
                        (Run.conversation_sequence < run.conversation_sequence)
                        if run.conversation_sequence is not None
                        else Run.id.is_not(None),
                    )
                    .limit(1)
                )
                if earlier:
                    # broker重投稍后会再通知，不因乱序到达跳过同会话旧任务。
                    return None
                conversation = session.scalar(
                    select(Conversation)
                    .where(Conversation.id == run.conversation_id)
                    .with_for_update()
                )
                if run.origin == "automatic":
                    from knowledgeops_agent.db import LearningTask

                    task = session.get(LearningTask, run.learning_task_id)
                    if (
                        not task
                        or task.status != "active"
                        or conversation.selected_learning_task_id != task.id
                    ):
                        run.status, run.stop_reason = "interrupted", "学习任务未启用，等待用户恢复"
                        return None
                if run.execution_sequence is None:
                    conversation.execution_sequence += 1
                    run.execution_sequence = conversation.execution_sequence
                if not run.continuation_bound:
                    run.continuation_snapshot_id = conversation.latest_snapshot_id
                    run.continuation_bound = True
            run.status, run.worker_id = "running", worker_id
            run.active_since = run.heartbeat_at = self.now()
            run.finished_at = None
            from knowledgeops_agent.db import LearningAdvance

            advance = session.scalar(
                select(LearningAdvance).where(LearningAdvance.run_id == run.id)
            )
            if advance:
                advance.status = "running"
            self._event(session, run, "started", {"status": "running"})
            return self._view(run)

    def heartbeat(self, run_id: str, worker_id: str) -> None:
        with self.sessions.begin() as session:
            run = self._locked(session, run_id)
            if run.worker_id != worker_id:
                raise PermissionError("worker owner does not match")
            if run.status == "running":
                run.heartbeat_at = self.now()

    def recover_on_startup(self) -> int:
        """worker 已取得独占进程租约后调用；不将遗留任务自动重新入队。

        崩溃前活跃时间计到最后一次心跳，进程停机期间不计入任务运行时间。
        心跳间隔内可能少计少量时间，网络超时仍单独限制单次请求。
        """
        with self.sessions.begin() as session:
            from knowledgeops_agent.run_control import lock_control

            lock_control(session)
            runs = list(
                session.scalars(
                    select(Run).where(Run.status.in_(["running", "queued"])).with_for_update()
                )
            )
            for run in runs:
                self._pause_clock(run, until=run.heartbeat_at)
                from knowledgeops_agent.learning_budget import settle_time

                settle_time(session, run)
                run.status, run.worker_id = "interrupted", None
                run.stop_reason = "服务重启，等待用户明确恢复"
                self._event(session, run, "interrupted", {"reason": run.stop_reason})
            # 即使崩溃恰好发生在两次Run之间，已激活的学习任务也不得自行恢复。
            from knowledgeops_agent.db import LearningTask

            for task in session.scalars(
                select(LearningTask)
                .where(LearningTask.mode == "delegated", LearningTask.status == "active")
                .with_for_update()
            ):
                task.status, task.stop_reason = "waiting", "服务重启，等待用户明确恢复"
            return len(runs)

    def _check(self, run: Run) -> None:
        if run.cancel_requested or run.status == "cancelled":
            raise RunStopped("cancelled")
        if run.status != "running":
            raise RunStopped(f"run is {run.status}")
        if self._active_ms(run) >= run.budgets["active_seconds"] * 1000:
            raise RunStopped("active time budget exhausted")

    def guard(self, run_id: str, *, worker_id: str | None = None) -> dict:
        """模型及工具执行前检查根运行；模型次数由独立账本再做原子预占。"""
        self._assert_live()
        with self.sessions() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise KeyError("run not found")
            if worker_id is not None and run.worker_id != worker_id:
                raise RunStopped("worker owner no longer matches")
            root = session.get(Run, run.root_id)
            self._check(root)
            from knowledgeops_agent.learning_budget import active_unit

            active_unit(session, root, self._active_ms(root))
            if run.id != root.id:
                self._check(run)
            return self._view(root)

    def publication_guard(
        self, session, run_id: str, worker_id: str, *, authority_epoch: int | None = None
    ) -> None:
        """与发布共用事务锁根任务，使用户取消与业务提交有明确的先后顺序。

        此方法不是工具参数，只有 Runtime 传入；Web 用户主动审阅发布是独立
        用户动作，可以发布已停止任务留下的草稿，不受该 Agent 活跃性限制。
        """
        run = self._active_root_guard(session, run_id, worker_id)
        if run.authority_epoch != run.applied_authority_epoch:
            raise SteeringPending("新的用户引导待处理，已阻止本次写入；先在模型安全点应用新指令。")
        if authority_epoch is not None and authority_epoch != run.authority_epoch:
            raise SteeringPending("该工具来自引导之前的旧决定，未执行；需要重新判断。")

    def _active_root_guard(self, session, run_id: str, worker_id: str) -> Run:
        """控制面更新也校验存活/租约，但允许自己处理尚未应用的引导。"""
        self._assert_live()
        run = self._locked(session, run_id)
        if run.parent_id is not None:
            raise PermissionError("research children cannot publish knowledge")
        if run.worker_id != worker_id:
            raise RunStopped("worker owner no longer matches")
        self._check(run)
        from knowledgeops_agent.learning_budget import active_unit

        active_unit(session, run, self._active_ms(run))
        return run

    def reserve_tool(self, run_id: str, tool_name: str, *, is_search: bool = False) -> None:
        """先计数再执行；失败也消耗次数，子任务共享根计数，不能各领一套额度。"""
        self._assert_live()
        with self.sessions.begin() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise KeyError("run not found")
            root = self._locked(session, run.root_id)
            self._check(root)
            if run.id != root.id:
                self._check(run)
            if root.tool_calls >= root.budgets["tool_calls"]:
                raise RunStopped("tool budget exhausted")
            if is_search and root.searches >= root.budgets["searches"]:
                raise RunStopped("search budget exhausted")
            from knowledgeops_agent.learning_budget import active_unit

            unit = active_unit(session, root, self._active_ms(root), lock=True)
            if unit is not None:
                if unit.status == "waiting":
                    raise RunStopped("知识单元正在等待真人决定，不能继续执行工具")
                if unit.tool_calls >= unit.budgets["tool_calls"]:
                    raise RunStopped("learning unit tool budget exhausted")
                if is_search and unit.searches >= unit.budgets["searches"]:
                    raise RunStopped("learning unit search budget exhausted")
                unit.tool_calls += 1
                unit.searches += int(is_search)
            root.tool_calls += 1
            root.searches += int(is_search)
            self._event(
                session,
                root,
                "tool_attempt",
                {"run_id": run_id, "tool": tool_name, "attempt": root.tool_calls},
            )

    def start_child(
        self,
        parent_id: str,
        task: str,
        *,
        operation_key: str | None = None,
        subagent: dict | None = None,
    ) -> dict:
        """只允许主任务创建最多两个活跃研究者；子任务输入强制只读、不可递归。"""
        with self.sessions.begin() as session:
            parent = self._locked(session, parent_id)
            if parent.parent_id is not None:
                raise PermissionError("recursive delegation is disabled")
            self._check(parent)
            if not parent.input_data["delegation"]:
                raise PermissionError("delegation is disabled")
            if not task.strip() or len(task) > 20000:
                raise ValueError("research task must contain 1..20000 characters")
            prior = None
            if operation_key:
                prior = session.scalar(select(Run).where(Run.request_key == operation_key))
                if prior:
                    if (
                        prior.parent_id != parent.id
                        or prior.task != task
                        or prior.input_data.get("subagent") != subagent
                    ):
                        raise ValueError("delegation key belongs to a different task")
                    if prior.status == "completed" or (
                        prior.status in TERMINAL and prior.generation == parent.generation
                    ):
                        return self._view(prior)
                    if prior.status == "running":
                        raise ValueError("research child is already running")
            active = list(
                session.scalars(
                    select(Run.id).where(Run.parent_id == parent_id, Run.status == "running")
                )
            )
            if len(active) >= 2:
                raise ValueError("at most two research children may run concurrently")
            if prior:
                # 只在根任务已由用户恢复到新代次后重启中断子任务，检查点与用量不重置。
                prior.status, prior.worker_id = "running", parent.worker_id
                prior.generation, prior.budgets = parent.generation, parent.budgets
                prior.cancel_requested, prior.stop_reason = False, None
                prior.active_since = prior.heartbeat_at = self.now()
                self._event(session, prior, "resumed", {"parent_generation": parent.generation})
                return self._view(prior)
            child_id = new_id()
            inputs = {**parent.input_data, "delegation": False, "publication_mode": "preview"}
            if subagent is not None:
                # 绑定由委派服务从根冻结目录校验后传入；模型参数不能提供整份定义。
                inputs["subagent"] = subagent
            child = Run(
                id=child_id,
                root_id=parent.root_id,
                parent_id=parent_id,
                origin="subagent",
                learning_task_id=parent.learning_task_id,
                learning_unit_id=parent.learning_unit_id,
                checkpoint_id=child_id,
                generation=parent.generation,
                request_key=operation_key,
                kind="research",
                conversation_id=parent.conversation_id,
                task=task,
                input_data=inputs,
                budgets=parent.budgets,
                status="running",
                worker_id=parent.worker_id,
                created_at=self.now(),
                active_since=self.now(),
                heartbeat_at=self.now(),
            )
            session.add(child)
            session.flush()
            self._event(session, child, "started", {"status": "running"})
            self._event(
                session,
                parent,
                "delegated",
                {
                    "child_id": child_id,
                    "task": task,
                    **(
                        {"agent_name": subagent["name"], "agent_revision": subagent["revision"]}
                        if subagent
                        else {}
                    ),
                },
            )
            return self._view(child)

    def cancel(self, run_id: str) -> dict:
        with self.sessions.begin() as session:
            run = self._locked(session, run_id)
            if run.status in TERMINAL:
                return self._view(run)
            run.cancel_requested = True
            if run.learning_unit_id and not run.parent_id:
                from knowledgeops_agent.db import LearningTask

                task = session.get(LearningTask, run.learning_task_id)
                if task.status == "active":
                    task.status, task.stop_reason = "paused", "用户取消当前运行，学习任务等待明确恢复"
            if run.status == "queued":
                run.status, run.stop_reason = "cancelled", "用户取消排队任务"
            self._event(session, run, "cancel_requested", {"status": run.status})
            return self._view(run)

    def finish(
        self,
        run_id: str,
        worker_id: str,
        *,
        status: str = "completed",
        outcome: dict | None = None,
        reason: str | None = None,
        continuation: dict | None = None,
    ) -> dict:
        """保存运行终态与用户结果；已提交业务数据不会被取消伪装为撤销。"""
        if status not in TERMINAL:
            raise ValueError("finish requires a terminal status")
        with self.sessions.begin() as session:
            run = self._locked(session, run_id)
            if run.worker_id != worker_id:
                raise PermissionError("worker owner does not match")
            if run.status in TERMINAL:
                return self._view(run)
            if run.cancel_requested:
                status, reason = "cancelled", "用户取消；已提交结果保留"
                outcome = {**(outcome or {}), "kind": "partial"}
            elif status == "completed" and run.authority_epoch != run.applied_authority_epoch:
                status, reason = "interrupted", "结束时仍有未应用引导，已保留进展；请核对后继续。"
                outcome = {**(outcome or {}), "kind": "partial"}
            elif (
                status == "completed"
                and self._active_ms(run) >= run.budgets["active_seconds"] * 1000
            ):
                status, reason = "budget_exhausted", "active time budget exhausted"
                outcome = {**(outcome or {}), "kind": "partial"}
            self._pause_clock(run)
            from knowledgeops_agent.learning_budget import settle_time

            settle_time(session, run)
            if outcome and outcome.get("draft_ids"):
                shown = list(
                    session.scalars(
                        select(ChangeSet).where(
                            (ChangeSet.root_id == run.id)
                            | (ChangeSet.publication_root_id == run.id),
                            ChangeSet.id.in_(outcome["draft_ids"]),
                        )
                    )
                )
                outcome = {
                    **outcome,
                    "draft_revisions": {draft.id: draft.revision for draft in shown},
                }
            run.status, run.outcome, run.stop_reason = status, outcome or {}, reason
            run.finished_at = self.now()
            from knowledgeops_agent.continuation import save_snapshot

            snapshot_id = save_snapshot(session, run, continuation)
            self._event(
                session,
                run,
                "finished",
                {
                    "status": status,
                    "outcome": run.outcome,
                    "reason": reason,
                    "continuation_snapshot_id": snapshot_id,
                },
            )
            if run.parent_id is None:
                for child in session.scalars(
                    select(Run)
                    .where(Run.parent_id == run.id, Run.status == "running")
                    .order_by(Run.id)
                    .with_for_update()
                ):
                    self._pause_clock(child)
                    child.status, child.stop_reason = "interrupted", "根任务停止，等待显式恢复"
                    self._event(session, child, "interrupted", {"reason": child.stop_reason})
            answer = run.outcome.get("answer")
            if answer and run.parent_id is None and run.conversation_id:
                session.add(
                    ChatMessage(
                        conversation_id=run.conversation_id,
                        run_id=run.id,
                        role="assistant",
                        content=answer,
                        user_sequence=run.conversation_sequence or 0,
                        created_at=self.now(),
                    )
                )
            from knowledgeops_agent.learning_coordinator import finished

            finished(session, run)
            return self._view(run)

    def resume(self, run_id: str, *, budgets: dict | None = None) -> dict:
        """仅由用户入口恢复。新预算是总上限，不重置已消费次数或活跃时长。"""
        with self.sessions.begin() as session:
            run = self._locked(session, run_id)
            if run.kind != "material" and run.format_version != 2:
                raise ValueError("旧格式运行没有可信资源/延续快照；保留成果并发送新任务继续。")
            if run.parent_id or run.status not in {
                "interrupted",
                "failed",
                "cancelled",
                "budget_exhausted",
            }:
                raise ValueError("only a stopped root run can be resumed")
            from knowledgeops_agent.db import MCPCall

            if session.scalar(
                select(MCPCall.id)
                .where(MCPCall.run_id == run.id, MCPCall.status == "pending")
                .limit(1)
            ):
                raise ValueError("请先允许或拒绝全部待确认的MCP调用，再恢复任务")
            limits = Budgets.model_validate({**run.budgets, **(budgets or {})}).model_dump()
            if run.learning_unit_id:
                from knowledgeops_agent.learning_budget import active_unit

                unit = active_unit(session, run, run.active_ms, lock=True)
                if unit and (
                    unit.model_calls >= unit.budgets["model_calls"]
                    or unit.tool_calls >= unit.budgets["tool_calls"]
                ):
                    raise ValueError("知识单元累计预算已耗尽，恢复不能清零")
            if (
                run.tool_calls >= limits["tool_calls"]
                or run.searches > limits["searches"]
                or run.active_ms >= limits["active_seconds"] * 1000
            ):
                raise ValueError("remaining budget required before resume")
            run.budgets = limits
            run.generation += 1
            from knowledgeops_agent.guidance import invalidate_on_resume

            invalidate_on_resume(self, session, run)
            session.add(
                RunDispatch(run_id=run.id, generation=run.generation, next_attempt_at=self.now())
            )
            run.status, run.cancel_requested = "queued", False
            run.stop_reason, run.worker_id = None, None
            self._event(session, run, "resumed", {"status": "queued", "budgets": limits})
            return self._view(run)

    def append_event(self, run_id: str, kind: str, data: dict) -> None:
        """调用方仅提交可展示、已脱敏的数据；模型密钥从不出现在运行上下文。"""
        with self.sessions.begin() as session:
            run = self._locked(session, run_id)
            self._event(session, run, kind, data)

    def events(self, run_id: str, *, after: int = 0, limit: int = 200) -> list[dict]:
        with self.sessions() as session:
            rows = session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.sequence > after)
                .order_by(RunEvent.sequence)
                .limit(max(1, min(limit, 1000)))
            )
            return [
                {
                    "sequence": row.sequence,
                    "kind": row.kind,
                    "data": row.data,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    def save_artifact(self, run_id: str, kind: str, content: str) -> str:
        """完整内容按哈希去重；只存运行产物，不伪装为 Agent 已读来源证据。"""
        digest = hashlib.sha256(content.encode()).hexdigest()
        with self.sessions.begin() as session:
            store = session.info.get("file_store")
            key = f"artifacts/{run_id}/{digest}.txt" if store else None
            if store is not None:
                store.put(key, content)
            session.execute(
                insert(cast(Table, RunArtifact.__table__))
                .values(
                    run_id=run_id,
                    kind=kind,
                    content_hash=digest,
                    content="" if store else content,
                    content_storage_key=key,
                )
                .on_conflict_do_nothing(constraint="uq_run_artifact_content")
            )
            return session.scalar(
                select(RunArtifact.id).where(
                    RunArtifact.run_id == run_id,
                    RunArtifact.kind == kind,
                    RunArtifact.content_hash == digest,
                )
            )

    def artifacts(self, run_id: str) -> list[dict]:
        with self.sessions() as session:
            return [
                {
                    "id": row.id,
                    "kind": row.kind,
                    "length": len(row.content),
                    "url": f"/api/runs/{run_id}/artifacts/{row.id}",
                }
                for row in session.scalars(
                    select(RunArtifact)
                    .where(RunArtifact.run_id == run_id)
                    .order_by(RunArtifact.created_at)
                )
            ]

    def read_artifact(
        self, artifact_id: str, *, root_id: str, start: int = 0, length: int = 6000
    ) -> dict:
        """按根任务校验访问；分段返回，防止补读工具再次把全部长结果塞进上下文。"""
        with self.sessions() as session:
            artifact = session.get(RunArtifact, artifact_id)
            if artifact is None:
                raise KeyError("run artifact not found")
            run = session.get(Run, artifact.run_id)
            if not self._artifact_allowed(session, run, root_id, artifact_id):
                raise PermissionError("artifact belongs to another root task")
            if not 0 <= start < len(artifact.content) or not 1 <= length <= 12000:
                raise ValueError("invalid artifact range")
            end = min(len(artifact.content), start + length)
            return {
                "artifact_id": artifact.id,
                "kind": artifact.kind,
                "text": artifact.content[start:end],
                "start": start,
                "end": end,
                "total_length": len(artifact.content),
                "next_start": end if end < len(artifact.content) else None,
            }

    def artifact_content(self, artifact_id: str, *, root_id: str) -> str:
        """内部原文装配入口，沿用同root权限；完整请求容量由统一网关最终检查。"""
        with self.sessions() as session:
            artifact = session.get(RunArtifact, artifact_id)
            if artifact is None:
                raise KeyError("run artifact not found")
            run = session.get(Run, artifact.run_id)
            if not self._artifact_allowed(session, run, root_id, artifact_id):
                raise PermissionError("artifact belongs to another root task")
            return artifact.content

    def _artifact_allowed(self, session, owner: Run, root_id: str, artifact_id: str) -> bool:
        """跨run只接受首次领取时冻结的继承清单，不按同会话扩大到任意新产物。"""
        if owner.root_id == root_id:
            return True
        root = session.get(Run, root_id)
        if root is None or not root.continuation_snapshot_id:
            return False
        from knowledgeops_agent.continuation import snapshot_view

        snapshot = session.get(ContinuationSnapshot, root.continuation_snapshot_id)
        if snapshot is None or snapshot.conversation_id != root.conversation_id:
            return False
        return artifact_id in snapshot_view(snapshot)["content"]["artifact_ids"]

    def continuation(self, run_id: str) -> dict | None:
        """读取已绑定的不可变父快照；未领取时不提前绑定，恢复也不会改指针。"""
        from knowledgeops_agent.continuation import snapshot_view

        with self.sessions() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise KeyError("run not found")
            if not run.continuation_snapshot_id:
                return None
            snapshot = session.get(ContinuationSnapshot, run.continuation_snapshot_id)
            if snapshot is None or snapshot.conversation_id != run.conversation_id:
                raise ValueError("bound continuation snapshot is unavailable")
            return snapshot_view(snapshot)

    def guide(self, queued_id: str, *, target_id: str, target_generation: int) -> dict:
        from knowledgeops_agent.guidance import request_guidance

        return request_guidance(
            self, queued_id, target_id=target_id, target_generation=target_generation
        )

    def guidance(self, root_id: str) -> list[dict]:
        from knowledgeops_agent.guidance import deliveries

        return deliveries(self, root_id)
