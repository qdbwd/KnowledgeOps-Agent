"""学习业务目标与知识单元。会话继续管理短期记忆，本服务不复制消息。

需求修订与操作修订分开：暂停不改变内容要求，修改目标则撤销旧范围的
确认。服务提供可信UI/意图入口；模型提出框架不等于获得自动发布许可。
"""

from __future__ import annotations

import builtins
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.persistence.models import (
    Conversation,
    LearningTask,
    LearningTaskRevision,
    LearningUnit,
    Run,
    utcnow,
)

UNIT_BUDGETS = {"model_calls": 40, "tool_calls": 80, "searches": 20, "active_seconds": 1200}


class UnitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(min_length=1, max_length=300)
    goal: str = Field(min_length=1, max_length=10000)
    dependencies: list[str] = Field(default_factory=list, max_length=50)


class TaskFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    goal: str = Field(min_length=1, max_length=10000)
    scope: str = Field(default="", max_length=10000)
    topic: str = Field(default="", max_length=200)
    mode: Literal["interactive", "delegated"] = "interactive"
    units: list[UnitInput] = Field(default_factory=list, max_length=50)


class TaskConfiguration(BaseModel):
    """用户对本任务的具体要求；不接受权限、预算或推断的掌握程度。"""

    model_config = ConfigDict(extra="forbid")
    purpose: str | None = Field(default=None, max_length=3000)
    background: str | None = Field(default=None, max_length=3000)
    focus: str | None = Field(default=None, max_length=3000)
    detail: str | None = Field(default=None, max_length=3000)
    examples: str | None = Field(default=None, max_length=3000)
    output: str | None = Field(default=None, max_length=3000)


def validate_framework(units: list[dict]) -> None:
    """要求有限的有向无环依赖；缺失前置或环都不能靠后台无限等待掩盖。"""
    by_key = {u["key"]: u for u in units}
    if len(by_key) != len(units):
        raise ValueError("知识单元标识不能重复")
    visited, visiting = set(), set()

    def visit(key):
        if key in visiting:
            raise ValueError("知识单元存在循环依赖")
        if key in visited:
            return
        if key not in by_key:
            raise ValueError("知识单元的前置依赖不存在")
        visiting.add(key)
        for dep in by_key[key]["dependencies"]:
            visit(dep)
        visiting.remove(key)
        visited.add(key)

    for key in by_key:
        visit(key)


def unit_view(row: LearningUnit) -> dict:
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "task_id",
            "key",
            "position",
            "title",
            "goal",
            "dependencies",
            "status",
            "budgets",
            "model_calls",
            "tool_calls",
            "searches",
            "active_ms",
            "reader_rounds",
            "review_rounds",
            "draft_id",
            "result_version_id",
            "stop_reason",
        )
    }


def task_view(session, row: LearningTask) -> dict:
    conversation = session.get(Conversation, row.conversation_id) if row.conversation_id else None
    return {
        "selected": bool(conversation and conversation.deletion_state == "active" and conversation.selected_learning_task_id == row.id),
        "conversation_available": bool(conversation and conversation.deletion_state == "active"),
        **{
            key: getattr(row, key)
            for key in (
                "id",
                "conversation_id",
                "revision",
                "requirements_revision",
                "title",
                "goal",
                "scope",
                "topic",
                "configuration",
                "mode",
                "status",
                "confirmed_requirements_revision",
                "auto_publish",
                "authority_epoch",
                "stop_reason",
            )
        },
        "units": [
            unit_view(unit)
            for unit in session.scalars(
                select(LearningUnit)
                .where(LearningUnit.task_id == row.id)
                .order_by(LearningUnit.position)
            )
        ],
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


class LearningTaskService:
    def __init__(self, sessions):
        self.sessions = sessions

    @staticmethod
    def _locked(session, task_id: str, expected_revision: int | None = None):
        lock_control(session)
        row = session.scalar(
            select(LearningTask).where(LearningTask.id == task_id).with_for_update()
        )
        if row is None:
            raise KeyError("学习任务不存在")
        if expected_revision is not None and row.revision != expected_revision:
            raise ValueError("学习任务修订已变化，请刷新后重试")
        return row

    @staticmethod
    def _record(session, row):
        row.updated_at = utcnow()
        session.flush()
        session.add(
            LearningTaskRevision(
                task_id=row.id, revision=row.revision, content=task_view(session, row)
            )
        )

    @staticmethod
    def _units(session, task, units):
        validate_framework(units)
        existing = {
            u.key: u
            for u in session.scalars(select(LearningUnit).where(LearningUnit.task_id == task.id))
        }
        removed = set(existing) - {u["key"] for u in units}
        for key in removed:
            row = existing[key]
            has_run = session.scalar(select(Run.id).where(Run.learning_unit_id == row.id).limit(1))
            if (
                row.status != "pending"
                or has_run
                or row.draft_id
                or row.model_calls
                or row.tool_calls
            ):
                raise ValueError("已有执行记录的单元不能删除，请保留并调整后续范围")
            session.delete(row)
        for index, value in enumerate(units):
            row = existing.get(value["key"])
            if row is None:
                row = LearningUnit(task_id=task.id, key=value["key"], budgets=dict(UNIT_BUDGETS))
                session.add(row)
            row.position, row.title, row.goal = index, value["title"], value["goal"]
            row.dependencies = value["dependencies"]
        session.flush()

    def create(self, *, conversation_id: str, request_key: str, **fields) -> dict:
        values = TaskFields.model_validate(fields).model_dump()
        if not request_key or len(request_key) > 200:
            raise ValueError("学习任务需要有效的幂等键")
        validate_framework(values["units"])
        with self.sessions.begin() as session:
            lock_control(session)
            conversation = session.scalar(
                select(Conversation).where(Conversation.id == conversation_id).with_for_update()
            )
            if conversation is None or conversation.deletion_state != "active":
                raise KeyError("会话不存在")
            old = session.scalar(
                select(LearningTask).where(LearningTask.request_key == request_key)
            )
            if old:
                if old.conversation_id != conversation_id or old.creation_input != values:
                    raise ValueError("幂等键已经用于不同的学习任务")
                return task_view(session, old)
            row = LearningTask(
                conversation_id=conversation_id,
                request_key=request_key,
                creation_input=values,
                **{k: v for k, v in values.items() if k != "units"},
            )
            session.add(row)
            session.flush()
            self._units(session, row, values["units"])
            if conversation.selected_learning_task_id is None:
                conversation.selected_learning_task_id = row.id
            self._record(session, row)
            return task_view(session, row)

    def get(self, task_id: str) -> dict:
        with self.sessions() as session:
            row = session.get(LearningTask, task_id)
            if row is None:
                raise KeyError("学习任务不存在")
            return task_view(session, row)

    def list(self, *, conversation_id: str | None = None) -> list[dict]:
        with self.sessions() as session:
            query = select(LearningTask).order_by(LearningTask.created_at.desc())
            if conversation_id:
                query = query.where(LearningTask.conversation_id == conversation_id)
            return [task_view(session, row) for row in session.scalars(query)]

    def history(self, task_id: str) -> builtins.list[dict]:
        with self.sessions() as session:
            return [
                row.content
                for row in session.scalars(
                    select(LearningTaskRevision)
                    .where(LearningTaskRevision.task_id == task_id)
                    .order_by(LearningTaskRevision.revision)
                )
            ]

    def update(self, task_id: str, *, expected_revision: int, **changes) -> dict:
        with self.sessions.begin() as session:
            row = self._locked(session, task_id, expected_revision)
            old = task_view(session, row)
            fields = {k: old[k] for k in TaskFields.model_fields}
            fields["units"] = [{k: u[k] for k in UnitInput.model_fields} for u in old["units"]]
            values = TaskFields.model_validate({**fields, **changes}).model_dump()
            if values == fields:
                return old
            self._units(session, row, values.pop("units"))
            for key, value in values.items():
                setattr(row, key, value)
            row.revision += 1
            row.requirements_revision += 1
            row.authority_epoch += 1
            row.auto_publish, row.confirmed_requirements_revision = False, None
            row.status, row.stop_reason = "draft", "任务要求已修改，委托执行需确认新范围"
            from knowledgeops_agent.learning.coordinator import stop_runs

            stop_runs(session, row)
            self._record(session, row)
            return task_view(session, row)

    def confirm(self, task_id: str, *, expected_revision: int, auto_publish: bool = False) -> dict:
        """可信用户确认范围。自动发布仍须通过草稿审读和版本检查。"""
        with self.sessions.begin() as session:
            row = self._locked(session, task_id, expected_revision)
            from knowledgeops_agent.app.lifecycle.access import require_conversation

            if row.conversation_id is None:
                raise ValueError("原会话已删除，请复用配置创建新的学习任务。")
            require_conversation(session, row.conversation_id)
            if row.mode == "delegated" and not session.scalar(
                select(LearningUnit.id).where(LearningUnit.task_id == row.id).limit(1)
            ):
                raise ValueError("委托整理需要先确定有限的知识单元")
            row.confirmed_requirements_revision = row.requirements_revision
            row.auto_publish, row.status, row.stop_reason = auto_publish, "active", None
            row.revision += 1
            row.authority_epoch += 1
            self._record(session, row)
            from knowledgeops_agent.learning.coordinator import advance

            advance(session, row.conversation_id)
            return task_view(session, row)

    def select(self, task_id: str, *, current_run_id: str | None = None) -> dict:
        """切换当前业务目标但不改聊天；旧任务的确认不会转给新任务。"""
        with self.sessions.begin() as session:
            lock_control(session)
            candidate = session.get(LearningTask, task_id)
            if candidate is None:
                raise KeyError("学习任务不存在")
            conversation = session.scalar(
                select(Conversation)
                .where(Conversation.id == candidate.conversation_id)
                .with_for_update()
            )
            if conversation is None or conversation.deletion_state != "active":
                raise ValueError("原会话已删除，请复用配置创建新的学习任务。")
            old_id = conversation.selected_learning_task_id
            if old_id and old_id != task_id:
                old = self._locked(session, old_id)
                if session.scalar(
                    select(Run.id)
                    .where(
                        Run.learning_task_id == old_id,
                        Run.id != current_run_id if current_run_id else Run.id.is_not(None),
                        Run.status.in_(["queued", "running"]),
                    )
                    .limit(1)
                ):
                    raise ValueError("当前任务仍有待执行或未结束运行，请先暂停并处理这些运行再切换")
                if old.status == "active":
                    old.status, old.stop_reason = "paused", "用户切换到另一学习任务"
                    old.authority_epoch += 1
                    old.revision += 1
                    self._record(session, old)
            conversation.selected_learning_task_id = task_id
            return task_view(session, self._locked(session, task_id))

    def configure(
        self, task_id: str, *, expected_revision: int, changes: dict, source: dict
    ) -> dict:
        """合并有来源的任务要求，不用语义提取结果覆盖人工角色文档。

        这些字段细化已有任务，不扩大目标、范围或工具权限。已确认范围仍有效，
        但内容要求修订变化会使旧审读失效；正在运行的普通配置快照不被改写。
        """
        values = TaskConfiguration.model_validate(changes).model_dump(exclude_unset=True)
        with self.sessions.begin() as session:
            row = self._locked(session, task_id, expected_revision)
            content = dict(row.configuration)
            for key, value in values.items():
                if value is None:
                    content.pop(key, None)
                else:
                    content[key] = {"value": value, "source": source}
            if content == row.configuration:
                return task_view(session, row)
            confirmed = row.confirmed_requirements_revision == row.requirements_revision
            row.configuration = content
            row.revision += 1
            row.requirements_revision += 1
            row.authority_epoch += 1
            if confirmed:
                row.confirmed_requirements_revision = row.requirements_revision
            self._record(session, row)
            return task_view(session, row)

    def control(self, task_id: str, *, expected_revision: int, action: str) -> dict:
        with self.sessions.begin() as session:
            row = self._locked(session, task_id, expected_revision)
            if action not in {"pause", "resume", "cancel"}:
                raise ValueError("未知学习任务操作")
            if action == "resume":
                from knowledgeops_agent.app.lifecycle.access import require_conversation

                if row.conversation_id is None:
                    raise ValueError("原会话已删除，请复用配置创建新的学习任务。")
                require_conversation(session, row.conversation_id)
            if (
                action == "resume"
                and row.confirmed_requirements_revision != row.requirements_revision
            ):
                raise ValueError("请先确认当前任务要求")
            row.status = {"pause": "paused", "resume": "active", "cancel": "cancelled"}[action]
            row.stop_reason = (
                None if action == "resume" else "用户" + {"pause": "暂停", "cancel": "取消"}[action]
            )
            row.authority_epoch += 1
            row.revision += 1
            self._record(session, row)
            from knowledgeops_agent.learning.coordinator import resume_task, stop_runs

            if action == "resume":
                resume_task(session, row)
            else:
                stop_runs(session, row, cancel=action == "cancel")
            return task_view(session, row)

    def reuse_configuration(
        self, task_id: str, *, expected_revision: int, source_task_id: str, source_revision: int
    ) -> dict:
        """用户预览后复制固定修订的任务要求；不复制执行状态、预算和授权。

        也允许选择本任务的旧修订作为回退。复用留下来源，但两任务后续独立编辑。
        人工角色正文仍通过原有Markdown导入/导出复用，不隐式覆盖。
        """
        from knowledgeops_agent.learning.intent import BoundSessions

        with self.sessions.begin() as session:
            self._locked(session, task_id, expected_revision)
            version = session.get(LearningTaskRevision, (source_task_id, source_revision))
            if version is None:
                raise KeyError("要复用的任务修订不存在")
            values = version.content.get("configuration", {})
            return LearningTaskService(BoundSessions(session)).configure(
                task_id,
                expected_revision=expected_revision,
                changes={
                    key: values.get(key, {}).get("value") for key in TaskConfiguration.model_fields
                },
                source={
                    "kind": "manual",
                    "reused_task_id": source_task_id,
                    "reused_revision": source_revision,
                },
            )
