"""读取已注册工具的真实模型接口；不构图、不执行工具、不发现外部服务。"""

from functools import lru_cache

from langchain_core.utils.function_calling import convert_to_openai_function

from knowledgeops_agent.harness.mcp.registry import MCPRegistry
from knowledgeops_agent.harness.skills.resources import FrozenSkillBackend
from knowledgeops_agent.harness.tools.catalog import mcp_tool_options, tool_options


@lru_cache(maxsize=8)
def _local_definitions(timeout: int) -> dict:
    """固定代码声明可复用；实际操作仍只能由有运行上下文的工具节点发起。"""
    from deepagents.middleware.subagents import TaskToolSchema

    from knowledgeops_agent.harness.runtime.agent import LOCAL_TOOLS
    from knowledgeops_agent.harness.sandbox.workspace import (
        WORKSPACE_TOOLS,
        build_workspace_middleware,
    )
    from knowledgeops_agent.harness.subagents.runtime import DELEGATION_TASK_DESCRIPTION
    from knowledgeops_agent.harness.tools.discovery import discovery_tool
    from knowledgeops_agent.vendor.deerflow.deferred_tools import DeferredToolCatalog

    backend = FrozenSkillBackend({})
    filesystem = build_workspace_middleware(backend, set(WORKSPACE_TOOLS), max_timeout=timeout)
    definitions = {
        item.name: convert_to_openai_function(item)
        for item in [
            *LOCAL_TOOLS.values(),
            *filesystem.tools,
            discovery_tool(DeferredToolCatalog(())),
        ]
    }
    # SDK公开参数模型不需要创建虚假的子角色；角色目录在真实运行中追加。
    definitions["task"] = {
        **convert_to_openai_function(TaskToolSchema),
        "name": "task",
        "description": DELEGATION_TASK_DESCRIPTION.format(
            available_agents="由本次运行冻结的角色目录提供；可在角色页面查看当前配置。"
        ),
    }
    return definitions


def tool_detail(sessions, identifier: str, *, timeout: int = 60) -> dict:
    """只接受目录身份；MCP定义来自已保存发现结果，配置地址和凭据不进入详情。"""
    if identifier.startswith("mcp:"):
        item = next(
            (
                row
                for row in mcp_tool_options(MCPRegistry(sessions).list())
                if row["id"] == identifier
            ),
            None,
        )
        if item is None:
            raise KeyError("工具不存在")
        return {
            **item,
            "definition": {
                "name": item["name"],
                "description": item["description"],
                "parameters": item["schema"],
            },
        }
    item = next((row for row in tool_options() if row["id"] == identifier), None)
    if item is None:
        raise KeyError("工具不存在")
    return {**item, "definition": _local_definitions(timeout)[item["name"]]}
