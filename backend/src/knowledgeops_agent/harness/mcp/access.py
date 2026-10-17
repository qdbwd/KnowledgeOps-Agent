"""MCP 的角色上限：复用原连接授权，主子构图和执行使用同一继承判断。"""

from knowledgeops_agent.harness.runtime.resources import ResourceUnavailable
from knowledgeops_agent.harness.subagents.runtime import selected_definition
from knowledgeops_agent.harness.tools.catalog import role_inherits_mcp


def role_allows_mcp(context) -> bool:
    if context.run_id == context.root_id:
        return True
    manifest = context.resources or {}
    try:
        definition = selected_definition(context, manifest)
    except ResourceUnavailable as error:
        raise PermissionError(str(error)) from error
    # 历史未绑定角色的研究任务原本允许只读 MCP，继续保持原合同。
    if definition is None:
        return True
    return manifest.get("child_mcp") == 1 and role_inherits_mcp(definition)


def check_access(context, binding: dict, item: dict) -> None:
    """不能凭当前服务配置向已冻结任务添加连接；恢复仍核对角色撤回。"""
    manifest = context.resources
    if manifest is not None and binding not in manifest.get("mcp", []):
        raise PermissionError("MCP连接不在根任务冻结快照中，请用新任务重新选择")
    if context.run_id != context.root_id:
        if item["permission"] != "read":
            raise PermissionError("子Agent仅能调用已审查的只读MCP工具")
        if not role_allows_mcp(context):
            raise PermissionError("角色工具组或旧任务快照未开放MCP能力")
