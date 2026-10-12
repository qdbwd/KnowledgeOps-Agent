"""供应商截断不能变成成功交付或可执行指令；只替换收费供应商。

真实图、检查点、业务服务及隔离账本验证停止、恢复与子任务边界。若移除
模型后检查或旧检查点的交付检查，应出现错误完成、工具执行或多余模型请求。
"""

import json
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from test_api_worker import services as _services
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

services = _services
runtime_services = _runtime_services
PARTIAL = "尚未完成的回答：先确认资料中的"


class CappedProvider:
    """截断发生在模型输出；后续正常响应使旧代码能暴露错误的继续执行。"""

    def __init__(self, shape="text", finish_reason="length"):
        self.shape, self.finish_reason = shape, finish_reason

    def bind_tools(self, tools):
        return self

    def stream(self, messages, **kwargs):
        if any(isinstance(item, ToolMessage) for item in messages):
            yield AIMessageChunk(
                content="计划已记录。", response_metadata={"finish_reason": "stop"}
            )
            return
        chunks = []
        if self.shape in {"tool", "invalid_tool"}:
            chunks = [
                {
                    "name": "update_plan",
                    "args": json.dumps({"steps": [{"step": "不应执行的计划", "status": "active"}]})
                    if self.shape == "tool"
                    else "not-json",
                    "id": "capped-plan",
                    "index": 0,
                }
            ]
        yield AIMessageChunk(
            content=PARTIAL,
            tool_call_chunks=chunks,
            response_metadata={"finish_reason": self.finish_reason},
            additional_kwargs={"reasoning_content": "供应商实际返回的片段"},
        )


@pytest.mark.parametrize("shape", ["text", "tool", "invalid_tool"])
def test_capped_worker_response_stops_without_tools_or_billable_retry(services, monkeypatch, shape):
    """保留原始响应及消费，同时禁止截断文本成功交付和截断调用执行。"""
    from knowledgeops_agent import worker

    services.gateway.enabled = True
    services.gateway.client = CappedProvider(shape)
    monkeypatch.setattr(worker, "ensure_publication_intent", lambda context: {})
    conversation = services.jobs.create_conversation("模型输出上限")
    run = services.jobs.enqueue(conversation["id"], "记录计划", request_key="capped-" + shape)
    saver = InMemorySaver()
    result = worker.execute_run(
        run["id"],
        run["generation"],
        services=services,
        worker_id="limit-worker",
        checkpointer=saver,
    )
    assert result["status"] == "failed"
    assert result["outcome"]["kind"] == "partial"
    assert result["outcome"]["answer"] == ""
    assert "输出上限" in result["stop_reason"]
    records = services.gateway.ledger.records(root_id=run["id"])
    assert len(records) == 1
    assert records[0]["response"]["content"] == PARTIAL
    assert records[0]["response"]["response_metadata"]["finish_reason"] == "length"
    assert (
        records[0]["response"]["additional_kwargs"]["reasoning_content"] == "供应商实际返回的片段"
    )
    events = services.jobs.events(run["id"])
    assert not any(event["kind"] == "tool_started" for event in events)
    assert any(event["kind"] == "model_output_limit" for event in events)
    assert services.knowledge.list_drafts(root_id=run["id"]) == []

    # 手动恢复同一失败检查点不会重发供应商请求，也不能绕过检查执行旧调用。
    resumed = services.jobs.resume(run["id"])
    second = worker.execute_run(
        run["id"],
        resumed["generation"],
        services=services,
        worker_id="limit-worker",
        checkpointer=saver,
    )
    assert second["status"] == "failed"
    assert "输出上限" in second["stop_reason"]
    assert services.gateway.ledger.records(root_id=run["id"]) == records
    assert not any(event["kind"] == "tool_started" for event in services.jobs.events(run["id"]))

    # 新任务继承的是背景，不应收到截断的有效/无效调用或原供应商调用包装。
    follow = services.jobs.enqueue(conversation["id"], "分段继续", request_key="next-" + shape)
    services.jobs.claim("next-worker", run_id=follow["id"], generation=follow["generation"])
    context = worker.RunContext(
        run_id=follow["id"],
        root_id=follow["id"],
        worker_id="next-worker",
        jobs=services.jobs,
        knowledge=services.knowledge,
        sources=services.sources,
        gateway=services.gateway,
    )
    messages = worker.initial_messages(context)
    assert not any(
        isinstance(item, AIMessage)
        and (item.tool_calls or item.invalid_tool_calls or item.additional_kwargs.get("tool_calls"))
        for item in messages
    )
    assert any("应用执行记录" in item.text and "输出上限" in item.text for item in messages)


@pytest.mark.parametrize("finish_reason", ["stop", None])
def test_normal_short_answer_is_not_guessed_to_be_truncated(services, monkeypatch, finish_reason):
    """判断来自供应商元数据，不能凭正文看起来未写完或缺少元数据就拒绝。"""
    from knowledgeops_agent import worker

    services.gateway.enabled = True
    services.gateway.client = CappedProvider(finish_reason=finish_reason)
    monkeypatch.setattr(worker, "ensure_publication_intent", lambda context: {})
    conversation = services.jobs.create_conversation()
    run = services.jobs.enqueue(conversation["id"], "回答问题", request_key="normal")
    result = worker.execute_run(
        run["id"],
        run["generation"],
        services=services,
        worker_id="normal-worker",
        checkpointer=InMemorySaver(),
    )
    assert result["status"] == "completed"
    assert result["outcome"]["answer"] == PARTIAL
    assert len(services.gateway.ledger.records(root_id=run["id"])) == 1


def test_capped_child_returns_failure_instead_of_successful_report(runtime_services):
    from knowledgeops_agent.delegation import delegate_one

    module, context, root = setup_run(runtime_services)
    context.gateway.client = CappedProvider()
    module.build_agent(context, checkpointer=InMemorySaver())
    definition = context.resources["subagents"][0]
    result = delegate_one(context, definition, "核对材料", "capped-child")
    assert result["status"] == "failed"
    assert result["answer"] == ""
    assert "输出上限" in result["error"]
    records = context.gateway.ledger.records(root_id=root["id"])
    assert len(records) == 1
    assert records[0]["response"]["content"] == PARTIAL


def test_next_turn_receives_capped_text_with_explicit_application_notice(runtime_services):
    """原片段仍可作为背景，但下一轮不能把它误当成上一轮完整交付。"""
    from knowledgeops_agent.continuation import from_state

    module, context, root = setup_run(runtime_services)
    state = {
        "messages": [
            HumanMessage(root["task"]),
            AIMessage(PARTIAL, response_metadata={"finish_reason": "length"}),
        ]
    }
    context.jobs.finish(
        root["id"],
        context.worker_id,
        status="failed",
        reason="输出上限",
        outcome={"kind": "partial", "answer": ""},
        continuation=from_state(context, state),
    )
    follow = context.jobs.enqueue(
        root["conversation_id"], "请分段继续", request_key="continue-capped"
    )
    context.jobs.claim(context.worker_id, run_id=follow["id"], generation=follow["generation"])
    messages = module.initial_messages(replace(context, run_id=follow["id"], root_id=follow["id"]))
    assert any(isinstance(item, AIMessage) and item.text == PARTIAL for item in messages)
    assert any(
        isinstance(item, HumanMessage) and "应用执行记录" in item.text and "输出上限" in item.text
        for item in messages[:-1]
    )
    assert messages[-1].text == "请分段继续"


def completed_old_graph(saver, checkpoint_id):
    """模拟旧版本已经结束、但尚未保存应用终态的真实 LangGraph 检查点。"""
    builder = StateGraph(MessagesState)
    builder.add_node(
        "old_model",
        lambda state: {
            "messages": [AIMessage(PARTIAL, response_metadata={"finish_reason": "length"})]
        },
    )
    builder.add_edge(START, "old_model")
    builder.add_edge("old_model", END)
    graph = builder.compile(checkpointer=saver)
    config = {"configurable": {"thread_id": checkpoint_id}}
    graph.invoke({"messages": [HumanMessage("旧任务")]}, config)
    assert not graph.get_state(config).next
    return graph


def test_completed_old_checkpoint_cannot_bypass_worker_output_check(services, monkeypatch):
    from knowledgeops_agent import worker

    conversation = services.jobs.create_conversation()
    run = services.jobs.enqueue(conversation["id"], "恢复旧结果", request_key="old-capped")
    saver = InMemorySaver()
    graph = completed_old_graph(saver, run["checkpoint_id"])
    monkeypatch.setattr(worker, "build_agent", lambda context, checkpointer: graph)
    monkeypatch.setattr(worker, "ensure_publication_intent", lambda context: {})
    result = worker.execute_run(
        run["id"], run["generation"], services=services, worker_id="old-worker", checkpointer=saver
    )
    assert result["status"] == "failed"
    assert result["outcome"]["answer"] == ""
    assert "输出上限" in result["stop_reason"]
    assert services.gateway.ledger.records() == []


def test_completed_old_child_checkpoint_cannot_return_truncated_report(
    runtime_services, monkeypatch
):
    from knowledgeops_agent.delegation import delegate_one

    module, context, root = setup_run(runtime_services)
    saver = InMemorySaver()
    module.build_agent(context, checkpointer=saver)
    definition = context.resources["subagents"][0]
    child = context.jobs.start_child(
        root["id"],
        "核对材料",
        operation_key=f"{root['id']}:old-capped-child:0",
        subagent=definition,
    )
    graph = completed_old_graph(saver, child["checkpoint_id"])
    # 模拟旧进程在图结束后退出；显式恢复根代次后才能再次领取原子任务。
    context.jobs.recover_on_startup()
    resumed = context.jobs.resume(root["id"])
    context.jobs.claim(context.worker_id, run_id=root["id"], generation=resumed["generation"])
    monkeypatch.setattr(module, "build_agent", lambda context, checkpointer: graph)
    result = delegate_one(context, definition, "核对材料", "old-capped-child")
    assert result["run_id"] == child["id"]
    assert result["status"] == "failed"
    assert result["answer"] == ""
    assert "输出上限" in result["error"]
    assert context.gateway.ledger.records() == []
