"""统一执行决定的纯函数；授权来自配置，不能来自模型理由或用户记忆。"""

from dataclasses import dataclass
from typing import Literal

from knowledgeops_agent.harness.permissions.rules import reusable_command, rule_matches


@dataclass(frozen=True)
class ExecutionDecision:
    decision: Literal["allow", "deny", "ask", "review"]
    reason: str
    rule_id: str | None = None


def evaluate_execution(
    tool_id: str,
    *,
    mode: str = "ask",
    available: bool = True,
    command: str = "",
    cwd: str = "",
    rules: list[dict] | None = None,
    mcp_permission: str | None = None,
) -> ExecutionDecision:
    """工具/角色撤回是上限。发布权限和 MCP 精确授权仍由原业务服务最终检查。"""
    from knowledgeops_agent.harness.tools.catalog import tool_options

    if not available:
        return ExecutionDecision("deny", "工具已停用、撤回或超出当前角色权限")
    if mode == "auto":
        mode = "ask"  # 历史冻结输入可读取，但不能继续授予旧自动执行权限。
    if mode not in {"ask", "auto_review"}:
        return ExecutionDecision("deny", "未知执行权限模式")
    if tool_id.startswith("mcp:"):
        if mcp_permission == "read":
            return ExecutionDecision("allow", "此MCP工具已明确授予只读许可")
        if mcp_permission == "confirm":
            return ExecutionDecision("review" if mode == "auto_review" else "ask",
                                     "此MCP工具需要逐次确认具体调用")
        return ExecutionDecision("deny", "此MCP工具没有执行许可")
    if tool_id not in {item["id"] for item in tool_options()}:
        return ExecutionDecision("deny", "工具不在有效能力目录中")
    if tool_id in {"middleware:write_file", "middleware:edit_file", "middleware:delete"}:
        if mode == "auto_review":
            return ExecutionDecision("review", "文件修改需要独立审查具体参数和目标版本")
        return ExecutionDecision("ask", "文件修改需要用户批准")
    if tool_id != "middleware:execute":
        return ExecutionDecision("allow", "已开放的本地能力；业务授权仍由原服务校验")
    matches = [row for row in (rules or []) if rule_matches(row, command, cwd=cwd)]
    # 明确拒绝和明确询问均优先于自动模式以及更宽泛的允许规则。
    for effect in ("deny", "ask", "allow"):
        for row in matches:
            if row["effect"] == effect:
                return ExecutionDecision(effect, "匹配用户配置的命令规则", row.get("id"))
    if reusable_command(command) is None and any(
        row.get("enabled", True) and row["cwd"] == cwd and row["effect"] in {"ask", "deny"}
        for row in (rules or [])
    ):
        return ExecutionDecision(
            "ask", "命令包含不能可靠展开的语法，无法证明符合已有拒绝/询问规则，需要逐次核对"
        )
    if mode == "auto_review":
        return ExecutionDecision("review", "此命令需要独立审查；不确定时交由用户确认")
    return ExecutionDecision("ask", "命令执行需要用户批准")
