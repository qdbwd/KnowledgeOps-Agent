"""只读预览新任务的工具选择：读取现有配置，不构图、不创建运行、不连接MCP。"""

from knowledgeops_agent.harness.mcp.registry import MCPRegistry
from knowledgeops_agent.harness.runtime.resources import load_skills
from knowledgeops_agent.harness.subagents.registry import SubagentRegistry
from knowledgeops_agent.harness.tools.catalog import (
    catalog_view,
    local_tool_reasons,
    role_inherits_mcp,
    role_tool_names,
)
from knowledgeops_agent.harness.tools.policy import ToolPolicyService

REASONS = {
    "globally_disabled": "全局已关闭",
    "main_not_selected": "主Agent没有选择此工具或所属工具组",
    "role_not_selected": "该角色没有选择此工具或所属工具组",
    "main_only": "只提供给主Agent，子角色不能写入或递归委派",
    "legacy_only": "仅用于恢复历史任务，新任务使用task委派",
    "knowledge_only": "仅知识库模式不允许联网或外部MCP",
    "auxiliary_disabled": "本次任务未开启辅助模型",
    "delegation_disabled": "本次任务未开启子Agent分工",
    "no_subagents": "没有已启用的子Agent角色，可先创建并启用角色",
    "no_skills": "该角色没有配置可加载的Skills",
    "role_disabled": "该角色已停用或删除",
    "connection_disabled": "MCP连接已停用",
    "permission_denied": "MCP工具尚未获得调用许可",
    "child_mcp": "子角色仅能继承根任务已允许的只读MCP工具",
    "workspace_disabled": "应用未配置本地文件工作区",
    "reader_disabled": "工作区工具依赖 read_file，读取工具已关闭或未被选择",
}


def effective_catalog(
    sessions,
    *,
    role_id: str | None = None,
    knowledge_only: bool = False,
    auxiliary: bool = False,
    delegation: bool = True,
    workspace_enabled: bool = False,
) -> dict:
    """预览已保存配置；真正执行仍冻结资源并重新核对许可、预算和业务条件。"""
    policy = ToolPolicyService(sessions).get()
    role = SubagentRegistry(sessions).get(role_id) if role_id else None
    has_subagents = bool(SubagentRegistry(sessions).snapshot()) if role is None else False
    root = {"knowledge_only": knowledge_only, "auxiliary": auxiliary, "delegation": delegation}
    is_root = role is None
    selected = role_tool_names(role) if role else None
    available_skills = {row["resource"].split("/")[2] for row in load_skills(policy)}
    has_skills = bool(available_skills & set(role["skills"])) if role else bool(available_skills)
    catalog = catalog_view(MCPRegistry(sessions).list())
    output = []
    for item in catalog["tools"]:
        reasons = list(item.get("unavailable_reasons", []))
        if role and (not role["enabled"] or role["deleted"]):
            reasons.append("role_disabled")
        if item["source"] == "mcp":
            if not is_root and item["permission"] != "read":
                reasons.append("child_mcp")
            if role and not role_inherits_mcp(role):
                reasons.append("role_not_selected")
            if knowledge_only:
                reasons.append("knowledge_only")
        else:
            reasons += local_tool_reasons(item, root, is_root=is_root, role_tools=selected)
            if is_root and item["name"] == "task" and not has_subagents:
                reasons.append("no_subagents")
            if policy["enabled"].get(item["id"]) is not True:
                reasons.append("globally_disabled")
            if (
                is_root
                and policy["resolved_main_tools"] is not None
                and item["id"] not in policy["resolved_main_tools"]
            ):
                reasons.append("main_not_selected")
            if (
                item["name"] == "read_file"
                and not has_skills
                and not (is_root and workspace_enabled)
            ):
                reasons.append("no_skills")
            if item["condition"] == "workspace":
                if not workspace_enabled:
                    reasons.append("workspace_disabled")
                selected_main = policy["resolved_main_tools"]
                if not policy["enabled"].get("middleware:read_file") or (
                    is_root
                    and selected_main is not None
                    and "middleware:read_file" not in selected_main
                ):
                    reasons.append("reader_disabled")
        reasons = list(dict.fromkeys(reasons))
        output.append(
            {
                **item,
                "available": not reasons,
                "unavailable_reasons": reasons,
                "unavailable_explanations": [REASONS[key] for key in reasons],
            }
        )
    skill_tool = next(row for row in output if row["id"] == "middleware:read_file")
    return {
        "policy_revision": policy["revision"],
        "role": {key: role[key] for key in ("id", "display_name", "revision", "enabled", "deleted")}
        if role
        else None,
        "task_options": root,
        "tools": output,
        "skills": {
            "available": skill_tool["available"] and has_skills,
            "explanation": "渐进Skills依赖read_file；核心知识库规则仍然生效。" if has_skills else "当前没有已启用且为该角色选择的技能。",
        },
    }
