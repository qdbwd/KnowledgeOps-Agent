"""Schema 延迟是执行边界：真实图、原MCP客户端和冻结权限共同验证。"""

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from test_mcp_business import configured, registry
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.harness.mcp.calls import MCPCalls, mcp_tools
from knowledgeops_agent.harness.runtime.resources import prepare_resources
from knowledgeops_agent.harness.tools.policy import ToolPolicyService
from knowledgeops_agent.vendor.deerflow.deferred_tools import (
    DeferredToolCatalog,
    DeferredToolFilterMiddleware,
    merge_promotions,
)

runtime_services = _runtime_services


def enable_discovery(context):
    # 本夹具只create_all而不迁移，显式建立新许可；不改变冻结的历史零版本。
    policy = ToolPolicyService(context.jobs.sessions)
    policy.update(expected_revision=policy.get()["revision"], enabled={"middleware:tool_search": True})


def tool(name, description="工具用途"):
    return StructuredTool(name=name, description=description,
                          args_schema={"type": "object", "properties": {}}, func=lambda: "ok")


def test_catalog_hash_ranking_and_run_scoped_parallel_promotion():
    catalog = DeferredToolCatalog((tool("browser_read", "browser 页面读取"),
                                   tool("note_read", "note 读取")))
    assert catalog.search("") == []
    assert catalog.search("browser 页面")[0].name == "browser_read"
    assert catalog.search("select:note_read")[0].name == "note_read"
    assert catalog.hash != DeferredToolCatalog((tool("note_read", "changed"),)).hash
    promoted = merge_promotions({"catalog_hash": catalog.hash, "run_id": "one", "names": ["browser_read"]},
                                {"catalog_hash": catalog.hash, "run_id": "one", "names": ["note_read"]})
    assert set(promoted["names"]) == set(catalog.names)
    state = {"promoted_tools": promoted}
    assert not DeferredToolFilterMiddleware(catalog, "one")._hidden(state)
    assert DeferredToolFilterMiddleware(catalog, "two")._hidden(state) == set(catalog.names)
    blocked = DeferredToolFilterMiddleware(catalog, "two")._blocked_tool_message(
        SimpleNamespace(state=state, tool_call={"name": "note_read", "id": "call"})
    )
    assert blocked.status == "error"
    assert "tool_search" in blocked.content


@pytest.mark.parametrize("revoke", [False, True])
def test_real_graph_search_loads_authorized_schema_then_rechecks_revocation(runtime_services, revoke):
    module, context, run = setup_run(runtime_services)
    binding = configured(context)
    enable_discovery(context)
    prepare_resources(context)
    name = mcp_tools(context)[0].name

    class Provider:
        rounds = 0
        visible = []

        def bind_tools(self, tools):
            self.visible = [item.name for item in tools]
            return self

        def stream(self, messages, **kwargs):
            self.rounds += 1
            if self.rounds == 1:
                assert name not in self.visible and "tool_search" in self.visible
                call, arguments = "tool_search", {"query": "get_note"}
            elif self.rounds == 2:
                assert name in self.visible
                observation = next(item for item in reversed(messages) if isinstance(item, ToolMessage))
                assert observation.name == "tool_search" and "topic" in observation.content
                if revoke:
                    registry(context).update(binding["id"], expected_revision=binding["revision"], enabled=False)
                call, arguments = name, {"topic": "Schema 加载后读取"}
            else:
                yield AIMessageChunk("本次工具检查结束。")
                return
            yield AIMessageChunk("", tool_call_chunks=[{
                "name": call, "args": json.dumps(arguments), "id": f"step-{self.rounds}", "index": 0,
            }])

    provider = Provider()
    context.gateway.client = provider
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    state = graph.invoke({"messages": module.initial_messages(context)},
                         {"configurable": {"thread_id": run["checkpoint_id"]}}, context=context)
    calls = MCPCalls(context).list()
    if revoke:
        assert calls == []
        assert any(isinstance(item, ToolMessage) and item.status == "error" for item in state["messages"])
    else:
        assert len(calls) == 1 and calls[0]["status"] == "succeeded"
    assert provider.rounds == 3
    assert state["promoted_tools"]["run_id"] == run["id"]


def test_unloaded_mcp_call_is_blocked_without_tool_reservation(runtime_services):
    module, context, run = setup_run(runtime_services)
    configured(context)
    enable_discovery(context)
    prepare_resources(context)
    name = mcp_tools(context)[0].name

    class Provider:
        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            if any(isinstance(item, ToolMessage) for item in messages):
                yield AIMessageChunk("未加载的工具未执行。")
            else:
                yield AIMessageChunk("", tool_call_chunks=[{
                    "name": name, "args": '{"topic":"不能绕过"}', "id": "bypass", "index": 0,
                }])

    context.gateway.client = Provider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    graph.invoke({"messages": module.initial_messages(context)},
                 {"configurable": {"thread_id": run["checkpoint_id"]}}, context=context)
    assert MCPCalls(context).list() == []
    assert context.jobs.get_run(run["id"])["tool_calls"] == 0
