"""将冻结工具策略接到公开模型/工具钩子；执行仍交给原SDK与业务服务。

构图限制初始能力，模型请求前收窄可见工具，执行前再次检查撤回。这里不调用
外部服务、不构造第二套MCP许可，也不把工具启用等同于知识发布授权。
"""

from knowledgeops_agent.harness.mcp.access import check_access
from knowledgeops_agent.harness.mcp.registry import MCPRegistry
from knowledgeops_agent.harness.tools.catalog import tool_options
from knowledgeops_agent.harness.tools.policy import ToolPolicyService, baseline_policy


def allowed_local_ids(context, *, is_root: bool | None = None) -> set[str]:
    """历史清单没有策略时只采用固定零版本，不能借恢复获得当前新增工具。"""
    binding = (context.resources or {}).get("tool_policy", baseline_policy())
    allowed = ToolPolicyService(context.jobs.sessions).allowed_ids(
        binding,
        is_root=context.run_id == context.root_id if is_root is None else is_root,
    )
    is_main = context.run_id == context.root_id if is_root is None else is_root
    workspace = (context.resources or {}).get("workspace")
    if not workspace or not is_main or "middleware:read_file" not in allowed:
        allowed -= {item["id"] for item in tool_options() if item["condition"] == "workspace"}
    if context.jobs.get_run(context.root_id)["input"].get("knowledge_only"):
        allowed.discard("middleware:execute")
    return allowed


def unavailable_tools(context) -> dict[str, str]:
    """返回本地策略和MCP原授权服务的拒绝理由，供模型请求与执行检查共同使用。"""
    allowed = allowed_local_ids(context)
    denied = {
        item["name"]: "工具已关闭、未被角色选择或本运行的许可已撤回"
        for item in tool_options()
        if item["id"] not in allowed
    }
    bindings = {row["id"]: row for row in (context.resources or {}).get("mcp", [])}
    registry = MCPRegistry(context.jobs.sessions)
    for item in context.tool_catalog or []:
        if item["source"] != "mcp":
            continue
        binding = bindings.get(item["connection_id"])
        if binding is None:
            denied[item["name"]] = "MCP工具缺少原连接绑定"
            continue
        try:
            external = registry.check(binding, item["original_name"])
            check_access(context, binding, external)
        except PermissionError as error:
            denied[item["name"]] = str(error)
    return denied


def filter_tools(context, tools) -> list:
    """只过滤已有SDK工具对象，不重写Schema或运行中引入新能力。"""
    if not tools:
        return []
    denied = unavailable_tools(context)
    registered = {row["name"] for row in context.tool_catalog or []}
    return [tool for tool in tools if tool.name in registered and tool.name not in denied]


def check_tool_call(context, name: str) -> None:
    """覆盖模型陈旧选择及恢复的待执行调用；拒绝在调用原handler前完成。"""
    reason = unavailable_tools(context).get(name)
    if reason:
        raise PermissionError(f"{name}不可用：{reason}")
    if name not in {row["name"] for row in context.tool_catalog or []}:
        raise PermissionError(f"{name}未在本运行的有效工具集合中")
