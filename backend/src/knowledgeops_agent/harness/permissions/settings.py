"""会话默认设置和命令规则的可信管理入口；不注册为 Agent 工具。"""

from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select

from knowledgeops_agent.app.lifecycle.access import require_conversation
from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.harness.permissions.rules import validate_rule
from knowledgeops_agent.persistence.models import Conversation, ExecutionRule, Run, utcnow


class RuntimeSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    expected_revision: int = Field(ge=0)
    permission_mode: Literal["ask", "auto"] | None = None
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=40)


def settings_view(conversation: Conversation) -> dict:
    return {
        "permission_mode": conversation.runtime_settings.get("permission_mode", "ask"),
        "reasoning_effort": conversation.runtime_settings.get("reasoning_effort", "default"),
        "revision": conversation.runtime_revision,
    }


def runtime_inputs(session, conversation: Conversation) -> dict:
    """创建根运行时固定配置来源；后续权限仍在工具边界读取实时策略。

    已校验的思考绑定来自用户设置入口；未设置时使用部署声明的默认能力，
    不在协调器事务里请求供应商。API 普通消息可以提供更新的元数据快照。
    """
    settings = settings_view(conversation)
    result = {"runtime_settings": settings}
    binding = conversation.runtime_settings.get("reasoning_binding")
    freeze = session.info.get("freeze_reasoning")
    if binding:
        result["reasoning"] = deepcopy(binding)
    elif freeze:
        result["reasoning"] = freeze(settings["reasoning_effort"])
    return result


def rule_view(row: ExecutionRule) -> dict:
    return {
        key: deepcopy(getattr(row, key))
        for key in (
            "id",
            "workspace",
            "conversation_id",
            "cwd",
            "argv",
            "match",
            "effect",
            "enabled",
            "revision",
        )
    }


class ExecutionSettings:
    """不含模型调用；API 负责校验供应商档位，这里负责事务和并发修订。"""

    def __init__(self, sessions, *, jobs=None):
        self.sessions = sessions
        self.jobs = jobs

    def get(self, conversation_id: str) -> dict:
        with self.sessions() as session:
            row = require_conversation(session, conversation_id)
            return settings_view(row)

    def update(
        self,
        conversation_id: str,
        *,
        expected_revision: int,
        reasoning_binding: dict | None = None,
        **fields,
    ) -> dict:
        data = RuntimeSettingsUpdate.model_validate(
            {"expected_revision": expected_revision, **fields}
        )
        with self.sessions.begin() as session:
            lock_control(session)
            row = session.scalar(
                select(Conversation).where(Conversation.id == conversation_id).with_for_update()
            )
            if row is None or row.deletion_state != "active":
                raise KeyError("conversation not found")
            if row.runtime_revision != data.expected_revision:
                raise ValueError("会话设置修订已变化，请刷新")
            changes = data.model_dump(exclude={"expected_revision"}, exclude_none=True)
            row.runtime_settings = {**row.runtime_settings, **changes}
            if reasoning_binding is not None:
                if reasoning_binding.get("requested") != changes.get("reasoning_effort"):
                    raise ValueError("思考参数与用户选择不一致")
                row.runtime_settings = {
                    **row.runtime_settings,
                    "reasoning_binding": reasoning_binding,
                }
            row.runtime_revision += 1
            if self.jobs is not None:
                for run in session.scalars(
                    select(Run)
                    .where(
                        Run.conversation_id == conversation_id,
                        Run.parent_id.is_(None),
                        Run.status.in_(["running", "interrupted", "queued"]),
                    )
                    .with_for_update()
                ):
                    self.jobs._event(
                        session,
                        run,
                        "execution_policy_changed",
                        {"runtime_revision": row.runtime_revision, **changes},
                    )
            return settings_view(row)

    def rules(self, workspace: str, conversation_id: str | None = None) -> list[dict]:
        with self.sessions() as session:
            rows = session.scalars(
                select(ExecutionRule)
                .where(
                    ExecutionRule.workspace == workspace,
                    ExecutionRule.deleted.is_(False),
                    or_(
                        ExecutionRule.conversation_id.is_(None),
                        ExecutionRule.conversation_id == conversation_id,
                    ),
                )
                .order_by(ExecutionRule.created_at, ExecutionRule.id)
            )
            return [rule_view(row) for row in rows]

    def create_rule(
        self,
        *,
        workspace: str,
        conversation_id: str | None,
        argv: list[str],
        match: str,
        cwd: str,
        effect: str,
        source_action_id: str | None = None,
    ) -> dict:
        validate_rule(workspace=workspace, cwd=cwd, argv=argv, match=match, effect=effect)
        with self.sessions.begin() as session:
            lock_control(session)
            if conversation_id is not None and session.get(Conversation, conversation_id) is None:
                raise KeyError("conversation not found")
            row = ExecutionRule(
                workspace=workspace,
                conversation_id=conversation_id,
                argv=argv,
                match=match,
                cwd=cwd,
                effect=effect,
                source_action_id=source_action_id,
            )
            session.add(row)
            session.flush()
            return rule_view(row)

    def update_rule(
        self, rule_id: str, *, workspace: str, expected_revision: int, **fields
    ) -> dict:
        if set(fields) - {"enabled", "effect", "argv", "match", "cwd", "deleted"}:
            raise ValueError("规则不能更换工作区或所属会话")
        if any(
            key in fields and not isinstance(fields[key], bool) for key in ("enabled", "deleted")
        ):
            raise ValueError("规则启停需要布尔值")
        with self.sessions.begin() as session:
            lock_control(session)
            row = session.scalar(
                select(ExecutionRule)
                .where(ExecutionRule.id == rule_id, ExecutionRule.workspace == workspace)
                .with_for_update()
            )
            if row is None or row.deleted:
                raise KeyError("command rule not found")
            if row.revision != expected_revision:
                raise ValueError("命令规则修订已变化，请刷新")
            new = {**rule_view(row), **fields}
            validate_rule(
                workspace=workspace, **{key: new[key] for key in ("argv", "match", "cwd", "effect")}
            )
            for key, value in fields.items():
                setattr(row, key, value)
            row.revision += 1
            row.updated_at = utcnow()
            return rule_view(row)

    def delete_rule(self, rule_id: str, *, workspace: str, expected_revision: int) -> None:
        self.update_rule(
            rule_id,
            workspace=workspace,
            expected_revision=expected_revision,
            deleted=True,
            enabled=False,
        )
