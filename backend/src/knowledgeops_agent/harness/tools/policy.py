"""统一本地工具策略：配置、修订和撤回，不替代MCP许可或知识发布授权。

借鉴DeerFlow工具组，将选择接到已有LangChain装配中。组只在保存时展开；
每个旧运行保存确定的策略，收窄增加逐工具代次，重新启用不能复活旧绑定。
本模块不包含模型、网络和工具执行，界面和Runtime共享同一查询服务。
"""

from copy import deepcopy

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text

from knowledgeops_agent.harness.runtime.resources import content_hash
from knowledgeops_agent.harness.tools.catalog import tool_options
from knowledgeops_agent.harness.tools.configuration import lock_configuration
from knowledgeops_agent.harness.tools.groups import (
    expand_group_ids,
    normalize_group_ids,
    select_groups,
)
from knowledgeops_agent.persistence.models import ToolPolicy, ToolPolicyRevision

POLICY_ID = "default"
# 已发布的零版本也是恢复合同：将来增加目录项不能改变旧零版本的许可。
# 新工具须通过当前配置显式启用；不能根据当前目录重新生成历史默认权限。
_BASELINE_IDS = (
    "builtin:search_knowledge",
    "builtin:list_knowledge",
    "builtin:knowledge_history",
    "builtin:read_knowledge",
    "builtin:list_materials",
    "builtin:inspect_material",
    "builtin:read_source",
    "builtin:read_tool_result",
    "builtin:update_plan",
    "builtin:call_model",
    "builtin:prepare_changes",
    "builtin:publish_changes",
    "builtin:capture_conversation",
    "builtin:list_changesets",
    "builtin:read_changeset",
    "builtin:revise_changeset",
    "builtin:reject_changeset",
    "builtin:delegate_research",
    "builtin:search_web",
    "builtin:read_web",
    "middleware:read_file",
    "middleware:task",
)


def baseline_policy() -> dict:
    """没有持久配置时的冻结零版本；GET不为了读取而写数据库。"""
    return {
        "id": POLICY_ID,
        "revision": 0,
        "selection_scope": "local_and_middleware",
        "enabled": dict.fromkeys(_BASELINE_IDS, True),
        "main_tools": None,
        "main_tool_groups": None,
        "resolved_main_tools": None,
        "tool_epochs": {},
        "main_epochs": {},
    }


class ToolPolicyUpdate(BaseModel):
    """省略选择表示不改，null恢复继承，空列表表示显式不选任何该类能力。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    expected_revision: int = Field(ge=0)
    enabled: dict[str, bool] = Field(default_factory=dict, max_length=200)
    skill_enabled: dict[str, bool] = Field(default_factory=dict, max_length=200)
    main_tools: list[str] | None = Field(default=None, max_length=200)
    main_tool_groups: list[str] | None = Field(default=None, max_length=100)

    @field_validator("enabled")
    @classmethod
    def known_switches(cls, value: dict) -> dict:
        if not set(value) <= {item["id"] for item in tool_options()}:
            raise ValueError("包含未知本地工具；MCP许可请使用原连接管理入口")
        return value

    @field_validator("main_tools")
    @classmethod
    def known_tools(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        selected = list(dict.fromkeys(item.strip() for item in value))
        if not all(selected) or not set(selected) <= {item["id"] for item in tool_options()}:
            raise ValueError("包含未知或不允许的本地工具ID")
        return selected

    @field_validator("main_tool_groups")
    @classmethod
    def known_groups(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else normalize_group_ids(value)


def _selected_ids(policy: dict, *, is_root: bool) -> set[str]:
    """已保存的许可上限；不从实时组成员推导，也不把外部MCP开关复制进来。"""
    allowed = {key for key, enabled in policy["enabled"].items() if enabled is True}
    if is_root and policy["resolved_main_tools"] is not None:
        allowed &= set(policy["resolved_main_tools"])
    return allowed


def read_policy(session) -> dict:
    """可复用调用方事务读取当前指针；运行资源冻结与其他资源保持同一事务。"""
    row = session.get(ToolPolicy, POLICY_ID)
    return deepcopy(row.content) if row is not None else baseline_policy()


def advance_main_epochs(current: dict, updated: dict) -> None:
    """选择收窄只撤回主 Agent，不撤回子角色的独立工具许可。"""
    old = current['resolved_main_tools']
    new = updated['resolved_main_tools']
    old_selection = set(current['enabled']) if old is None else set(old)
    new_selection = set(updated['enabled']) if new is None else set(new)
    for identifier in old_selection - new_selection:
        updated['main_epochs'][identifier] = current['main_epochs'].get(identifier, 0) + 1


def save_policy(session, current: dict, updated: dict) -> None:
    """调用方持有配置锁；当前指针与不可变修订必须和关联组/角色一起提交。"""
    updated['revision'] = current['revision'] + 1
    row = session.get(ToolPolicy, POLICY_ID)
    if row is None:
        row = ToolPolicy(id=POLICY_ID, revision=updated['revision'], content=deepcopy(updated))
        session.add(row)
        session.flush()
        session.add(ToolPolicyRevision(policy_id=POLICY_ID, revision=0, content=deepcopy(current)))
    else:
        row.revision, row.content = updated['revision'], deepcopy(updated)
    session.add(ToolPolicyRevision(policy_id=POLICY_ID, revision=updated['revision'], content=deepcopy(updated)))


class ToolPolicyService:
    def __init__(self, sessions):
        self.sessions = sessions

    def get(self) -> dict:
        with self.sessions() as session:
            return read_policy(session)

    def history(self) -> list[dict]:
        with self.sessions() as session:
            rows = list(
                session.scalars(
                    select(ToolPolicyRevision)
                    .where(ToolPolicyRevision.policy_id == POLICY_ID)
                    .order_by(ToolPolicyRevision.revision)
                )
            )
            return [deepcopy(row.content) for row in rows] if rows else [baseline_policy()]

    def update(self, *, expected_revision: int, **fields) -> dict:
        data = ToolPolicyUpdate.model_validate(
            {"expected_revision": expected_revision, **fields}
        ).model_dump(exclude_unset=True)
        data.pop("expected_revision")
        with self.sessions.begin() as session:
            lock_configuration(session)
            # 单应用配置更新很少；沿用角色目录的PG表锁，包含首写时还没有行的竞争。
            session.execute(text("LOCK TABLE tool_policies IN SHARE ROW EXCLUSIVE MODE"))
            current = read_policy(session)
            if current["revision"] != expected_revision:
                raise ValueError("工具策略修订已变化，请刷新")
            updated = deepcopy(current)
            # 技能开关与工具策略共用版本/历史；部署兼容性是上限，UI不能越过它。
            skill_switches = data.pop("skill_enabled", {})
            if skill_switches:
                from knowledgeops_agent.core.resources import resource_path
                from knowledgeops_agent.harness.runtime.resources import SKILLS_PATH, skill_catalog
                from knowledgeops_agent.harness.skills.management import SkillManagement
                from knowledgeops_agent.persistence.models import SkillPackageState

                SkillManagement(self.sessions, resource_path("SKILLS_DIRECTORY", SKILLS_PATH))._recover_in(session)
                catalog = {item["name"]: item for item in skill_catalog()}
                for name, enabled in skill_switches.items():
                    managed = session.get(SkillPackageState, name)
                    if managed is not None and managed.removed:
                        raise ValueError("技能已移除，请先重新导入")
                    if name not in catalog:
                        raise ValueError("技能不存在，请刷新目录")
                    if enabled and not catalog[name]["enabled"]:
                        raise ValueError("技能因部署条件未满足而不可用，不能通过用户开关启用")
                    switches = updated.setdefault("skill_enabled", {})
                    epochs = updated.setdefault("skill_epochs", {})
                    if switches.get(name, True) and not enabled:
                        epochs[name] = epochs.get(name, 0) + 1
                    switches[name] = enabled
            switches = data.pop("enabled", {})
            for identifier, enabled in switches.items():
                if current["enabled"].get(identifier, False) and not enabled:
                    updated["tool_epochs"][identifier] = (
                        current["tool_epochs"].get(identifier, 0) + 1
                    )
                updated["enabled"][identifier] = enabled
            if "main_tools" in data or "main_tool_groups" in data:
                updated.update(data)
                groups, tools = updated["main_tool_groups"], updated["main_tools"]
                select_groups(groups or [], session)
                updated["resolved_main_tools"] = (
                    None
                    if groups is None and tools is None
                    else list(dict.fromkeys([*(tools or []), *expand_group_ids(groups or [], session)]))
                )
                advance_main_epochs(current, updated)
            save_policy(session, current, updated)
            return deepcopy(updated)

    def allowed_ids(self, binding: dict, *, is_root: bool) -> set[str]:
        """旧策略只会收窄；先核对完整历史，再与当前许可及撤回代次求交。"""
        if binding.get("id") != POLICY_ID or type(binding.get("revision")) is not int:
            raise PermissionError("工具策略绑定不兼容")
        with self.sessions() as session:
            version = (
                session.get(ToolPolicyRevision, (POLICY_ID, binding["revision"]))
                if binding["revision"]
                else None
            )
            original = (
                version.content
                if version is not None
                else baseline_policy()
                if binding["revision"] == 0
                else None
            )
            if original is None or content_hash(binding) != content_hash(original):
                raise PermissionError("工具策略与冻结历史不符")
            current = read_policy(session)
        return {
            identifier
            for identifier in _selected_ids(binding, is_root=is_root)
            & _selected_ids(current, is_root=is_root)
            if binding["tool_epochs"].get(identifier, 0)
            == current["tool_epochs"].get(identifier, 0)
            and (
                not is_root
                or binding["main_epochs"].get(identifier, 0)
                == current["main_epochs"].get(identifier, 0)
            )
        }
