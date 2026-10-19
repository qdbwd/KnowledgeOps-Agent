"""用户管理的只读子 Agent 目录；PG 修订和撤销代次沿用 MCPRegistry 的事务模式。

字段契约参考 DeerFlow 5ecc1c2 managed_subagents/base.py，使用标准 Pydantic
校验而不引入其存储抽象、模型选择或工具黑名单；本项目使用正向许可目录。
角色定义不是发布授权，也不能扩大根任务的网络、辅助模型或预算选择。
"""

from __future__ import annotations

import builtins
from copy import deepcopy

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator
from sqlalchemy import func, select, text

from knowledgeops_agent.harness.runtime.resources import content_hash, load_skills
from knowledgeops_agent.harness.skills.packages import is_skill_entry
from knowledgeops_agent.harness.skills.presentation import present_skill
from knowledgeops_agent.harness.tools.catalog import (
    role_inherits_mcp,
    tool_options,
)
from knowledgeops_agent.harness.tools.configuration import lock_configuration
from knowledgeops_agent.harness.tools.groups import (
    expand_group_ids,
    normalize_group_ids,
    read_groups,
    select_groups,
)
from knowledgeops_agent.persistence.models import SubagentDefinition, SubagentDefinitionRevision

BUILTIN_ID = "builtin-researcher"
MAX_CUSTOM_DEFINITIONS = 20


def options(session=None) -> dict:
    """目录来自已有工具名称和项目 Skill 元数据，不读取任意文件或连接外部服务。"""
    skills = []
    for row in load_skills():
        if is_skill_entry(row):
            metadata = yaml.safe_load(row["content"].split("---", 2)[1])
            skills.append(present_skill({"name": row["name"], "description": metadata["description"]}))
    return {
        "tools": tool_options(child_only=True),
        "tool_groups": read_groups(session, child_only=True),
        "skills": skills,
    }


def _required_text(value: str) -> str:
    if not value.strip():
        raise ValueError("角色文本不能为空白")
    return value.strip()


def _selected_names(values: list[str], kind: str) -> list[str]:
    """只接受当前受控目录；去空白及重复条目不会提升任何许可。"""
    selected = list(dict.fromkeys(value.strip() for value in values))
    # 不再保留一份启动时复制的工具名单，展示与配置校验共同读取目录。
    allowed = (
        {row["name"] for row in tool_options(child_only=True)}
        if kind == "tools"
        else {row["name"] for row in options()["skills"]}
    )
    if not all(selected) or not set(selected) <= allowed:
        raise ValueError("角色包含未知或不允许的工具/技能")
    return selected


def _resolve_tools(tools: list[str], groups: list[str] | None, session=None) -> list[str]:
    """只在选择发生变化时展开组，保留显式工具；普通提示词编辑不能自动扩权。"""
    names = {row["id"]: row["name"] for row in tool_options(child_only=True)}
    select_groups(groups or [], session, child_only=True)
    return list(
        dict.fromkeys(
            [*tools, *(names[key] for key in expand_group_ids(groups or [], session, child_only=True))]
        )
    )


class SubagentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    display_name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    system_prompt: str = Field(min_length=1, max_length=8000)
    tools: list[str] = Field(default_factory=list, max_length=len(tool_options(child_only=True)))
    tool_groups: list[str] | None = Field(default=None, max_length=100)
    skills: list[str] = Field(default_factory=list, max_length=100)
    enabled: bool = False

    @field_validator("display_name", "description", "system_prompt")
    @classmethod
    def required_text(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("tools", "skills")
    @classmethod
    def selected_names(cls, value: list[str], info: ValidationInfo) -> list[str]:
        return _selected_names(value, info.field_name or "")

    @field_validator("tool_groups")
    @classmethod
    def groups(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else normalize_group_ids(value)


class SubagentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    expected_revision: int = Field(ge=1)
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, min_length=1, max_length=500)
    system_prompt: str | None = Field(default=None, min_length=1, max_length=8000)
    tools: list[str] | None = Field(default=None, max_length=len(tool_options(child_only=True)))
    tool_groups: list[str] | None = Field(default=None, max_length=100)
    skills: list[str] | None = Field(default=None, max_length=100)
    enabled: bool | None = None
    deleted: bool = False

    @model_validator(mode="before")
    @classmethod
    def no_null_updates(cls, value):
        if isinstance(value, dict) and any(
            item is None and key != "tool_groups" for key, item in value.items()
        ):
            raise ValueError("修改字段不能为null；不修改时请省略该字段")
        return value

    @field_validator("display_name", "description", "system_prompt")
    @classmethod
    def required_text(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("tools", "skills")
    @classmethod
    def selected_names(cls, value: list[str], info: ValidationInfo) -> list[str]:
        return _selected_names(value, info.field_name or "")

    @field_validator("tool_groups")
    @classmethod
    def groups(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else normalize_group_ids(value)


# 已发布的内置模板是冻结协议：目录增删不改变旧版本，修改模板须新增修订并
# 保留旧项。工具/Skills显式列举，不能从实时目录推导许可或悄悄升级旧运行。
_BUILTIN_REVISIONS: dict[int, dict] = {
    1: {
        "id": BUILTIN_ID,
        "name": "researcher",
        "display_name": "只读研究者",
        "description": "按需读取指定材料，研究独立问题并返回证据与未决项。",
        "system_prompt": "你是只读研究子 Agent，仅完成委派问题，返回简洁结论、读取工具提供的引用和未决项。不能维护或发布知识。",
        "tools": [
            "search_knowledge",
            "list_knowledge",
            "knowledge_history",
            "read_knowledge",
            "list_materials",
            "inspect_material",
            "read_source",
            "read_tool_result",
            "update_plan",
            "search_web",
            "read_web",
            "call_model",
        ],
        "skills": ["evidence-check", "guided-learning", "knowledge-maintenance"],
        "enabled": True,
        "deleted": False,
        "revision": 1,
        "authorization_epoch": 0,
        "builtin": True,
    },
}


def builtin_definition(revision: int | None = None) -> dict:
    """仅服务历史读取/恢复；新任务不再自动安装内置角色。"""
    selected = max(_BUILTIN_REVISIONS) if revision is None else revision
    return deepcopy(_BUILTIN_REVISIONS[selected])


def definition_view(row: SubagentDefinition) -> dict:
    view = {
        "id": row.id,
        "name": row.name,
        "display_name": row.display_name,
        "description": row.description,
        "system_prompt": row.system_prompt,
        "tools": row.tools,
        "skills": row.skills,
        "enabled": row.enabled,
        "deleted": row.deleted,
        "revision": row.revision,
        "authorization_epoch": row.authorization_epoch,
        "builtin": False,
    }
    # 0014旧行和历史修订没有这些字段；首次读取不能改写旧定义的哈希形状。
    if row.resolved_tools is not None:
        view.update(tool_groups=row.tool_groups, resolved_tools=row.resolved_tools)
    return view


def active_definitions(session) -> list[dict]:
    return [
        definition_view(row)
        for row in session.scalars(
            select(SubagentDefinition)
            .where(SubagentDefinition.enabled.is_(True), SubagentDefinition.deleted.is_(False))
            .order_by(SubagentDefinition.created_at, SubagentDefinition.id)
        )
    ]


def role_templates() -> list[dict]:
    """编辑起点，不是活动角色；显式保存后沿普通目录生成独立身份与修订。"""
    catalog = options()
    allowed_tools = {row["name"] for row in catalog["tools"]}
    allowed_skills = {row["name"] for row in catalog["skills"]}
    fields = {
        "name": "researcher",
        "display_name": "只读研究者",
        "description": "按需读取材料，研究独立问题并返回证据与未决项。",
        "system_prompt": "完成分配的研究问题，核对来源与适用条件，返回简明结论、引用和未决项。需要材料时先检索再读取；不维护或发布知识，不扩大任务范围。",
        "tools": [name for name in (
            "search_knowledge", "read_knowledge", "list_materials", "inspect_material",
            "read_source", "read_tool_result", "search_web", "read_web",
        ) if name in allowed_tools],
        "tool_groups": [],
        "skills": [name for name in ("evidence-check",) if name in allowed_skills],
        "enabled": False,
    }
    return [{"id": "researcher", "display_name": fields["display_name"],
             "description": fields["description"], "fields": fields}]


class SubagentRegistry:
    def __init__(self, sessions):
        self.sessions = sessions

    def options(self) -> dict:
        with self.sessions() as session:
            return options(session)

    @staticmethod
    def _save(session, row) -> None:
        session.flush()
        session.add(
            SubagentDefinitionRevision(
                definition_id=row.id, revision=row.revision, content=definition_view(row)
            )
        )

    def list(self) -> builtins.list[dict]:
        with self.sessions() as session:
            return [
                definition_view(row)
                for row in session.scalars(
                    select(SubagentDefinition)
                    .where(SubagentDefinition.deleted.is_(False))
                    .order_by(SubagentDefinition.created_at, SubagentDefinition.id)
                )
            ]

    def get(self, identifier: str) -> dict:
        if identifier == BUILTIN_ID:
            return builtin_definition()
        with self.sessions() as session:
            row = session.get(SubagentDefinition, identifier)
            if row is None or row.deleted:
                raise KeyError("子Agent角色不存在")
            return definition_view(row)

    def create(self, **fields) -> dict:
        data = SubagentCreate.model_validate(fields).model_dump()
        with self.sessions.begin() as session:
            lock_configuration(session)
            data["resolved_tools"] = _resolve_tools(data["tools"], data["tool_groups"], session)
            # 配置写入很少；数据库表锁使并发创建也不能突破总数，不新增配额基础层。
            session.execute(text("LOCK TABLE subagent_definitions IN SHARE ROW EXCLUSIVE MODE"))
            count = session.scalar(
                select(func.count())
                .select_from(SubagentDefinition)
                .where(SubagentDefinition.deleted.is_(False))
            )
            if count >= MAX_CUSTOM_DEFINITIONS:
                raise ValueError("最多保留20个未删除的自定义子Agent角色")
            if session.scalar(
                select(SubagentDefinition.id).where(SubagentDefinition.name == data["name"])
            ):
                raise ValueError("角色名称已被使用；删除后的名称也保留历史身份")
            row = SubagentDefinition(**data)
            session.add(row)
            self._save(session, row)
            return definition_view(row)

    def update(self, identifier: str, *, expected_revision: int, **fields) -> dict:
        if identifier == BUILTIN_ID:
            raise PermissionError("旧researcher定义仅用于历史恢复；请用模板创建可管理的角色")
        data = SubagentUpdate.model_validate(
            {"expected_revision": expected_revision, **fields}
        ).model_dump(exclude_unset=True)
        data.pop("expected_revision")
        deleted = data.pop("deleted", False)
        with self.sessions.begin() as session:
            lock_configuration(session)
            row = session.scalar(
                select(SubagentDefinition)
                .where(SubagentDefinition.id == identifier)
                .with_for_update()
            )
            if row is None or row.deleted:
                raise KeyError("子Agent角色不存在")
            if row.revision != expected_revision:
                raise ValueError("子Agent角色修订已变化，请刷新")
            previous_tools = row.tools if row.resolved_tools is None else row.resolved_tools
            resolved = (
                _resolve_tools(
                    data.get("tools", row.tools), data.get("tool_groups", row.tool_groups), session
                )
                if "tools" in data or "tool_groups" in data
                else list(previous_tools)
            )
            revoke = (
                deleted
                or (row.enabled and data.get("enabled") is False)
                or not set(previous_tools) <= set(resolved)
                or (role_inherits_mcp({"tool_groups": row.tool_groups})
                    and not role_inherits_mcp({"tool_groups": data.get("tool_groups", row.tool_groups)}))
                or ("skills" in data and not set(row.skills) <= set(data["skills"]))
            )
            for key, value in data.items():
                setattr(row, key, value)
            row.resolved_tools = resolved
            if deleted:
                row.deleted, row.enabled = True, False
            if revoke:
                row.authorization_epoch += 1
            row.revision += 1
            self._save(session, row)
            return definition_view(row)

    def history(self, identifier: str) -> builtins.list[dict]:
        if identifier == BUILTIN_ID:
            return [builtin_definition(revision) for revision in sorted(_BUILTIN_REVISIONS)]
        with self.sessions() as session:
            if session.get(SubagentDefinition, identifier) is None:
                raise KeyError("子Agent角色不存在")
            return [
                row.content
                for row in session.scalars(
                    select(SubagentDefinitionRevision)
                    .where(SubagentDefinitionRevision.definition_id == identifier)
                    .order_by(SubagentDefinitionRevision.revision)
                )
            ]

    def snapshot(self) -> builtins.list[dict]:
        with self.sessions() as session:
            return active_definitions(session)

    def validate_binding(self, binding: dict) -> dict:
        """普通编辑保留旧修订；撤销代次和不可变原文同时匹配才可继续使用旧绑定。"""
        if not isinstance(binding, dict) or not isinstance(binding.get("id"), str):
            raise PermissionError("子Agent冻结定义不兼容")
        if type(binding.get("revision")) is not int or binding["revision"] < 1:
            raise PermissionError("子Agent冻结修订不兼容")
        if binding["id"] == BUILTIN_ID:
            try:
                template = builtin_definition(binding["revision"])
            except KeyError:
                raise PermissionError("内置子Agent冻结修订不存在") from None
            if content_hash(binding) != content_hash(template):
                raise PermissionError("内置子Agent冻结定义不兼容")
            return binding
        with self.sessions() as session:
            current = session.get(SubagentDefinition, binding["id"])
            version = session.get(SubagentDefinitionRevision, (binding["id"], binding["revision"]))
            if version is None or content_hash(version.content) != content_hash(binding):
                raise PermissionError("子Agent冻结定义与历史修订不符")
            if (
                current is None
                or current.deleted
                or not current.enabled
                or binding.get("enabled") is not True
                or binding.get("deleted") is not False
                or current.authorization_epoch != binding["authorization_epoch"]
            ):
                raise PermissionError("子Agent角色或许可已撤回；请用新任务重新选择")
            return binding
