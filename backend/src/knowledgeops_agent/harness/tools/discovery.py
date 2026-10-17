"""把摘取的延迟目录接入本应用权限、工具结果和持久图状态。"""

import json
from typing import Any

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_function
from langgraph.types import Command

from knowledgeops_agent.harness.tools.access import filter_tools
from knowledgeops_agent.vendor.deerflow.deferred_tools import (
    DeferredToolCatalog,
    DeferredToolFilterMiddleware,
)


def build_discovery(context, external, *, search_enabled: bool) -> list:
    """只对新容量合同的 MCP 延迟；旧快照维持旧可见性，不改历史待执行调用。"""
    if not external or not context.gateway.capacity.deferred_mcp_tools or not search_enabled:
        return []
    catalog = DeferredToolCatalog(tuple(external))

    @tool
    def tool_search(query: str, runtime: ToolRuntime[Any]) -> Command:
        """在本任务已授权的外部 MCP 工具目录中查询并加载完整参数定义。

        尚未加载的 MCP 工具不能调用。用关键词描述所需动作，如“浏览器 页面”；
        或用 select:完整工具名,完整工具名 精确选择。keywords: 前缀可选；普通输入
        不解释为正则。关键词取前256字符、最多16词，按覆盖与名称命中返回最多5项。
        只查询冻结目录，不联网安装工具；没找到时改用目录工具名称/原始描述关键词。
        返回 Schema 之后再按其中参数调用；停用、只读限制和原审批仍然有效。
        """
        available = DeferredToolCatalog(tuple(filter_tools(runtime.context, catalog.tools)))
        matched = available.search(query)
        names = [item.name for item in matched]
        content = json.dumps({
            "tools": [convert_to_openai_function(item) for item in matched],
            "available_count": len(available.tools),
            "hint": "未命中时可用服务名、动作或英文原始工具名查询；搜索不扩大权限。",
        }, ensure_ascii=False)
        return Command(update={
            "promoted_tools": {"catalog_hash": catalog.hash, "run_id": runtime.context.run_id,
                               "names": names, "recent": names},
            "messages": [ToolMessage(content=content, name="tool_search", tool_call_id=runtime.tool_call_id)],
        })

    return [DeferredToolFilterMiddleware(catalog, context.run_id,
                                        tool_search if search_enabled else None)]
