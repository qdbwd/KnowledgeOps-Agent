"""摘取 DeerFlow 8d994cc1 的关键词排序、目录哈希与延迟 Schema 过滤。

MIT 许可及具体改动见同目录 README-deferred.md / LICENSE。本地适配增加
Run 范围与并行归并；搜索不访问网络，也不修改已冻结工具的 Schema。
"""

import hashlib
import json
from dataclasses import dataclass
from functools import cached_property

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_function


@dataclass(frozen=True)
class DeferredToolCatalog:
    tools: tuple[BaseTool, ...]

    @cached_property
    def names(self) -> frozenset[str]:
        return frozenset(tool.name for tool in self.tools)

    @cached_property
    def hash(self) -> str:
        canon = [{"name": tool.name, "schema": convert_to_openai_function(tool)}
                 for tool in sorted(self.tools, key=lambda tool: tool.name)]
        blob = json.dumps(canon, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def search(self, query: str) -> list[BaseTool]:
        query = query.strip()
        if query.startswith("select:"):
            wanted = {name.strip() for name in query[7:].split(",")}
            return [tool for tool in self.tools if tool.name in wanted]
        if query.startswith("keywords:"):
            query = query[len("keywords:"):]
        terms = tuple(dict.fromkeys(query[:256].casefold().split()))[:16]
        scored: list[tuple[tuple[int, int], BaseTool]] = []
        for candidate in self.tools:
            name, description = candidate.name.casefold(), (candidate.description or "").casefold()
            name_hits = sum(term in name for term in terms)
            coverage = sum(term in name or term in description for term in terms)
            if coverage:
                scored.append(((coverage, name_hits), candidate))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [candidate for _, candidate in scored[:5]]


def merge_promotions(previous: dict | None, update: dict) -> dict:
    """同轮多个搜索只归并同目录/Run；跨运行不继承可调用集合。"""
    previous = previous or {}
    if any(previous.get(key) != update.get(key) for key in ("catalog_hash", "run_id")):
        return dict(update)
    return {**update, "names": sorted(set(previous.get("names", [])) | set(update.get("names", [])))}


class DeferredToolFilterMiddleware(AgentMiddleware):
    def __init__(self, catalog: DeferredToolCatalog, run_id: str, search_tool=None):
        super().__init__()
        self.catalog, self.run_id = catalog, run_id
        self.tools = [search_tool] if search_tool is not None else []

    def _promoted(self, state) -> set[str]:
        promoted = (state or {}).get("promoted_tools", {})
        if promoted.get("catalog_hash") == self.catalog.hash and promoted.get("run_id") == self.run_id:
            return set(promoted.get("names", []))
        return set()

    def _hidden(self, state) -> set[str]:
        return set(self.catalog.names) - self._promoted(state)

    def _filter_tools(self, request):
        hidden = self._hidden(request.state)
        return request.override(tools=[tool for tool in request.tools if tool.name not in hidden])

    def _blocked_tool_message(self, request):
        name = request.tool_call.get("name")
        if name not in self._hidden(request.state):
            return None
        return ToolMessage(
            content=f"未执行：{name} 的 Schema 尚未加载。先用 tool_search 查询并加载，再决定是否调用。",
            tool_call_id=request.tool_call["id"], name=name, status="error",
        )

    def wrap_model_call(self, request, handler):
        return handler(self._filter_tools(request))

    def wrap_tool_call(self, request, handler):
        blocked = self._blocked_tool_message(request)
        return blocked if blocked is not None else handler(request)

    async def awrap_model_call(self, request, handler):
        return await handler(self._filter_tools(request))

    async def awrap_tool_call(self, request, handler):
        blocked = self._blocked_tool_message(request)
        return blocked if blocked is not None else await handler(request)
