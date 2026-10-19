"""本地工具组维护：先核对影响，再原子更新组与关联配置，不改旧任务快照。

借鉴 DeerFlow 的组筛选与角色正向选择；其 YAML 组不提供本应用需要的
PG 修订/撤回语义，因此复用本项目策略和角色服务，未引入另一套执行器。
"""

import builtins
from copy import deepcopy

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from knowledgeops_agent.harness.runtime.resources import content_hash
from knowledgeops_agent.harness.tools.catalog import group_options, tool_options
from knowledgeops_agent.harness.tools.configuration import GroupSelectionError, lock_configuration
from knowledgeops_agent.persistence.models import (
    SubagentDefinition,
    ToolGroup,
    ToolGroupRevision,
    new_id,
)

GROUP_DESCRIPTIONS = {
    "knowledge_read": "检索、列出和阅读知识文档及历史版本。",
    "materials_read": "查找材料、目录与引用，并阅读保存的正文。",
    "run_support": "读取工具结果、维护计划、查找外部工具和询问用户。",
    "task_continuity": "查找会话原话并维护当前任务要点。",
    "long_term_memory": "检索和核对当前任务允许使用的长期记忆。",
    "learning": "读取学习草稿、征求反馈并保存待确认的学习安排。",
    "auxiliary": "调用辅助模型，仍须任务显式开启并受共享预算约束。",
    "knowledge_maintenance": "管理知识草稿与发布；发布仍需独立业务授权。",
    "delegation": "创建、查看、等待及管理只读子任务。",
    "web": "搜索和读取公开网页，仍受任务联网选择约束。",
    "skills": "按需读取已允许的技能说明和固定资源。",
    "files_read": "列出、匹配和检索工作区文件。",
    "files_write": "新建、编辑和删除工作区普通文件。",
    "local_execution": "在配置的工作区执行有时间限制的命令。",
    "workspace_delivery": "导入材料、交付固定文件和同步知识工作文件。",
}


def default_groups() -> list[dict]:
    """供新空目录与迁移基线使用；正常数据库的已存行（含墓碑）优先。"""
    return [
        {**group, "description": GROUP_DESCRIPTIONS[group["id"]], "revision": 1, "deleted": False}
        for group in group_options()
    ]


def group_view(row: ToolGroup) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "tool_ids": list(row.tool_ids),
        "revision": row.revision,
        "deleted": row.deleted,
    }


def read_groups(
    session=None, *, child_only: bool = False, include_deleted: bool = False
) -> list[dict]:
    """GET 不写库；迁移种子覆盖旧静态组，删除墓碑永远覆盖同名默认项。"""
    groups = {row["id"]: row for row in default_groups()}
    if session is not None:
        groups.update(
            {
                row.id: group_view(row)
                for row in session.scalars(
                    select(ToolGroup).order_by(ToolGroup.created_at, ToolGroup.id)
                )
            }
        )
    result = [deepcopy(row) for row in groups.values() if include_deleted or not row["deleted"]]
    if not child_only:
        return result
    allowed = {row["id"] for row in tool_options(child_only=True)}
    filtered = []
    for row in result:
        full = row["tool_ids"]
        row["tool_ids"] = [key for key in full if key in allowed]
        row["excluded_tool_ids"] = [key for key in full if key not in allowed]
        # 默认只供主 Agent 的组不挤占角色选项。自定义或修改过的组保留说明，
        # 即使只剩不允许的工具，也只能得到空集合，不能变成继承全部工具。
        if (
            row["tool_ids"]
            or row["id"] not in GROUP_DESCRIPTIONS
            or row["revision"] > 1
            or not full
        ):
            filtered.append(row)
    filtered.append(
        {
            "id": "mcp",
            "name": "继承只读 MCP",
            "tool_ids": [],
            "description": "继承根任务已允许的只读 MCP，不新增连接或写入许可。",
        }
    )
    return filtered


def normalize_group_ids(groups: list[str]) -> list[str]:
    selected = list(dict.fromkeys(group.strip() for group in groups))
    if not all(selected) or any(len(group) > 100 for group in selected):
        raise ValueError("工具组身份不能为空或超过100字符")
    return selected


def select_groups(groups: list[str], session=None, *, child_only: bool = False) -> list[str]:
    selected = normalize_group_ids(groups)
    allowed = {row["id"] for row in read_groups(session, child_only=child_only)}
    if not set(selected) <= allowed:
        raise GroupSelectionError("包含未知、已删除或不允许的工具组，请刷新目录")
    return selected


def expand_group_ids(
    groups: list[str], session=None, *, child_only: bool = False, catalog: list[dict] | None = None
) -> list[str]:
    """只返回本地稳定 ID，始终与子角色白名单求交；恢复只读取已冻结展开值。"""
    rows = read_groups(session) if catalog is None else catalog
    members = {
        key
        for row in rows
        if row["id"] in groups and not row.get("deleted")
        for key in row["tool_ids"]
    }
    return [row["id"] for row in tool_options(child_only=child_only) if row["id"] in members]


class GroupValidation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    @field_validator("name", "description", check_fields=False)
    @classmethod
    def trim(cls, value, info):
        value = value.strip()
        if info.field_name == "name" and not value:
            raise ValueError("工具组名称不能为空白")
        return value

    @field_validator("tool_ids", check_fields=False)
    @classmethod
    def members(cls, value):
        result = list(dict.fromkeys(value))
        if not set(result) <= {row["id"] for row in tool_options()}:
            raise ValueError("工具组只接受现有本地工具；MCP请使用连接管理")
        return result


class GroupFields(GroupValidation):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    tool_ids: list[str] = Field(default_factory=list, max_length=200)


class GroupEdit(GroupValidation):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    tool_ids: list[str] | None = Field(default=None, max_length=200)
    expected_revision: int = Field(ge=1)
    deleted: bool = False

    @model_validator(mode="before")
    @classmethod
    def no_null(cls, value):
        if isinstance(value, dict) and any(item is None for item in value.values()):
            raise ValueError("不修改的字段请省略，不接受null")
        return value


class GroupUpdate(GroupEdit):
    preview_token: str = Field(pattern=r"^[a-f0-9]{64}$")


class GroupDelete(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)
    preview_token: str = Field(pattern=r"^[a-f0-9]{64}$")


class ToolGroupService:
    def __init__(self, sessions):
        self.sessions = sessions

    def list(self) -> list[dict]:
        with self.sessions() as session:
            return read_groups(session)

    def create(self, **fields) -> dict:
        data = GroupFields.model_validate(fields).model_dump()
        with self.sessions.begin() as session:
            lock_configuration(session)
            if len(read_groups(session)) >= 100:
                raise ValueError("最多保留100个工具组")
            row = ToolGroup(id=new_id(), **data, revision=1, deleted=False)
            session.add(row)
            session.flush()
            session.add(ToolGroupRevision(group_id=row.id, revision=1, content=group_view(row)))
            return group_view(row)

    @staticmethod
    def _impact(
        session, identifier: str, expected_revision: int, changes: dict
    ) -> tuple[dict, dict, builtins.list[SubagentDefinition]]:
        from knowledgeops_agent.harness.subagents.registry import definition_view
        from knowledgeops_agent.harness.tools.policy import read_policy

        groups = read_groups(session)
        previous = next((row for row in groups if row["id"] == identifier), None)
        if previous is None:
            raise KeyError("工具组不存在或已删除")
        if previous["revision"] != expected_revision:
            raise ValueError("工具组修订已变化，请刷新并重新预览")
        proposed = {**previous, **changes}
        proposed["revision"] += 1
        catalog = [proposed if row["id"] == identifier else row for row in groups]
        policy = read_policy(session)
        roles = list(
            session.scalars(
                select(SubagentDefinition)
                .where(SubagentDefinition.deleted.is_(False))
                .order_by(SubagentDefinition.created_at, SubagentDefinition.id)
            )
        )
        affected = []
        membership_changed = proposed["deleted"] or previous["tool_ids"] != proposed["tool_ids"]

        def selection(old_groups):
            return (
                [key for key in old_groups if key != identifier]
                if proposed["deleted"]
                else list(old_groups)
            )

        def entry(kind, identity, name, revision, before, after, chosen):
            return {
                "kind": kind,
                "id": identity,
                "name": name,
                "revision": revision,
                "added": sorted(set(after) - set(before)),
                "removed": sorted(set(before) - set(after)),
                "tool_groups": chosen,
                "resolved_tool_ids": after,
            }

        if identifier in (policy["main_tool_groups"] or []):
            chosen = selection(policy["main_tool_groups"])
            before = policy["resolved_main_tools"] or []
            after = (
                list(
                    dict.fromkeys(
                        [*(policy["main_tools"] or []), *expand_group_ids(chosen, catalog=catalog)]
                    )
                )
                if membership_changed
                else list(before)
            )
            affected.append(
                entry("main", "main", "主 Agent", policy["revision"], before, after, chosen)
            )
        relevant = []
        child_ids = {row["name"]: row["id"] for row in tool_options(child_only=True)}
        for row in roles:
            if identifier not in (row.tool_groups or []):
                continue
            relevant.append(row)
            chosen = selection(row.tool_groups)
            before = [
                child_ids[name]
                for name in (row.resolved_tools if row.resolved_tools is not None else row.tools)
            ]
            after = (
                list(
                    dict.fromkeys(
                        [
                            *[child_ids[name] for name in row.tools],
                            *expand_group_ids(chosen, child_only=True, catalog=catalog),
                        ]
                    )
                )
                if membership_changed
                else list(before)
            )
            affected.append(
                entry("subagent", row.id, row.display_name, row.revision, before, after, chosen)
            )
        token = content_hash(
            {
                "before": previous,
                "proposed": proposed,
                "groups": groups,
                "policy": policy,
                "roles": [definition_view(row) for row in relevant],
            }
        )
        return {"group": proposed, "affected": affected, "preview_token": token}, policy, relevant

    def preview(self, identifier: str, *, expected_revision: int, **fields) -> dict:
        changes = GroupEdit.model_validate(
            {"expected_revision": expected_revision, **fields}
        ).model_dump(exclude_unset=True)
        changes.pop("expected_revision")
        with self.sessions.begin() as session:
            lock_configuration(session, shared=True)
            return self._impact(session, identifier, expected_revision, changes)[0]

    def update(
        self, identifier: str, *, expected_revision: int, preview_token: str, **fields
    ) -> dict:
        from knowledgeops_agent.harness.subagents.registry import SubagentRegistry
        from knowledgeops_agent.harness.tools.policy import advance_main_epochs, save_policy

        changes = GroupUpdate.model_validate(
            {"expected_revision": expected_revision, "preview_token": preview_token, **fields}
        ).model_dump(exclude_unset=True)
        changes.pop("expected_revision")
        changes.pop("preview_token")
        with self.sessions.begin() as session:
            lock_configuration(session)
            impact, policy, roles = self._impact(session, identifier, expected_revision, changes)
            if impact["preview_token"] != preview_token:
                raise ValueError("工具组影响已变化，请重新预览后保存")
            proposed = impact["group"]
            row = session.get(ToolGroup, identifier)
            if row is None:
                seed = next(group for group in default_groups() if group["id"] == identifier)
                row = ToolGroup(**seed)
                session.add(row)
                session.flush()
                session.add(
                    ToolGroupRevision(group_id=identifier, revision=seed["revision"], content=seed)
                )
            for key, value in proposed.items():
                setattr(row, key, value)
            session.add(
                ToolGroupRevision(
                    group_id=identifier, revision=row.revision, content=deepcopy(proposed)
                )
            )
            affected = {item["id"]: item for item in impact["affected"]}
            if "main" in affected:
                selection = affected["main"]
                updated = deepcopy(policy)
                updated["main_tool_groups"] = selection["tool_groups"]
                updated["resolved_main_tools"] = selection["resolved_tool_ids"]
                advance_main_epochs(policy, updated)
                save_policy(session, policy, updated)
            names = {tool["id"]: tool["name"] for tool in tool_options(child_only=True)}
            for role in roles:
                selection = affected[role.id]
                role.tool_groups = selection["tool_groups"]
                role.resolved_tools = [names[key] for key in selection["resolved_tool_ids"]]
                if selection["removed"]:
                    role.authorization_epoch += 1
                role.revision += 1
                SubagentRegistry._save(session, role)
            return deepcopy(proposed)
