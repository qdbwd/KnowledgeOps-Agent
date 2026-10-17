"""真实图、真实本地MCP、真实业务回执；仅主模型选择由离线替身提供。"""

import json

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_mcp_business import configured
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

runtime_services = _runtime_services


def test_mcp_tool_runtime_injection_and_pending_checkpoint_resume(runtime_services):
    module, context, run = setup_run(runtime_services)
    configured(context, "confirm")
    from knowledgeops_agent.harness.mcp.calls import MCPApprovalRequired, MCPCalls, mcp_tools
    from knowledgeops_agent.harness.runtime.resources import prepare_resources

    class Provider:
        name = ""
        calls = 0

        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            self.calls += 1
            if any(isinstance(message, ToolMessage) for message in messages):
                yield AIMessageChunk("已收到材料，仍需核对来源。")
            else:
                yield AIMessageChunk(
                    "",
                    tool_call_chunks=[
                        {
                            "name": self.name,
                            "args": json.dumps({"topic": "图中真实调用"}),
                            "id": "graph-mcp-call",
                            "index": 0,
                        }
                    ],
                )

    provider = Provider()
    context.gateway.client = provider
    prepare_resources(context)
    provider.name = mcp_tools(context)[0].name
    saver = InMemorySaver()
    graph = module.build_agent(context, checkpointer=saver)
    config = {"configurable": {"thread_id": run["checkpoint_id"]}}
    with pytest.raises(MCPApprovalRequired):
        graph.invoke({"messages": module.initial_messages(context)}, config=config, context=context)
    assert graph.get_state(config).next  # 工具节点未完成，仍有可恢复的执行位置。
    receipt = MCPCalls(context).list()[0]
    assert receipt["status"] == "pending" and provider.calls == 1
    context.jobs.finish(
        run["id"], context.worker_id, status="interrupted", outcome={"kind": "waiting_approval"}
    )
    MCPCalls(context).decide(receipt["id"], expected_hash=receipt["input_hash"], approve=True)
    assert context.jobs.get_run(run["id"])["status"] == "queued"
    MCPCalls(context).decide(receipt["id"], expected_hash=receipt["input_hash"], approve=True)
    assert context.jobs.get_run(run["id"])["generation"] == 2
    context.jobs.claim(context.worker_id, run_id=run["id"], generation=2)
    graph = module.build_agent(context, checkpointer=saver)
    state = graph.invoke(None, config=config, context=context)
    assert MCPCalls(context).list()[0]["status"] == "succeeded"
    result = next(message for message in state["messages"] if isinstance(message, ToolMessage))
    assert json.loads(result.content)["source_version_id"]
    assert provider.calls == 2  # 恢复先接着执行工具，没有重新请求模型猜测同一操作。
