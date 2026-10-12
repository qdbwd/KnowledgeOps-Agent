"""最终回复用真实证据修正格式；没有 ID 时返回预算内修订反馈。"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_runtime import runtime_services, setup_run  # noqa: F401

from knowledgeops_agent.response_validation import FinalAnswerValidation


def test_final_verified_marker_is_rendered_without_losing_protocol_metadata(runtime_services):  # noqa: F811
    _, context, run = setup_run(runtime_services)
    source = context.sources.import_text("固定材料", "重复任务需要幂等。")
    evidence = context.sources.read_segment(source["segments"][0]["id"], root_id=run["id"])
    message = AIMessage(
        content=f"结论[citation:{evidence['id']}]",
        id="final-message",
        additional_kwargs={"reasoning_content": "保留供应商协议字段"},
    )
    update = FinalAnswerValidation().after_model(
        {"messages": [message]}, SimpleNamespace(context=context)
    )
    repaired = update["messages"][0]
    assert repaired.content == f"结论[来源](/api/evidence/{evidence['id']})"
    assert repaired.id == message.id
    assert repaired.additional_kwargs == message.additional_kwargs
    assert not update["final_response_feedback"]
    assert context.gateway.ledger.records(root_id=run["id"]) == []


def test_naked_final_marker_requires_bounded_repair_even_with_valid_link(runtime_services):  # noqa: F811
    _, context, run = setup_run(runtime_services)
    source = context.sources.import_text("固定材料", "重复任务需要幂等。")
    evidence = context.sources.read_segment(source["segments"][0]["id"], root_id=run["id"])
    message = AIMessage(content="结论[citation]" + evidence["citation"])
    update = FinalAnswerValidation().after_model(
        {"messages": [message]}, SimpleNamespace(context=context)
    )
    assert update["jump_to"] == "model"
    assert update["answer_repair_count"] == 1
    assert update["final_response_feedback"]
    with pytest.raises(ValueError, match="two repairs"):
        FinalAnswerValidation().after_model(
            {"messages": [message], "answer_repair_count": 2}, SimpleNamespace(context=context)
        )


def test_foreign_task_marker_is_not_converted_to_a_valid_link(runtime_services):  # noqa: F811
    _, context, _ = setup_run(runtime_services)
    source = context.sources.import_text("其他任务材料", "其他任务的证据。")
    evidence = context.sources.read_segment(source["segments"][0]["id"], root_id="other-task")
    update = FinalAnswerValidation().after_model(
        {"messages": [AIMessage(content=f"结论[citation:{evidence['id']}]")]},
        SimpleNamespace(context=context),
    )
    assert update["jump_to"] == "model"
    assert "messages" not in update


def test_real_graph_replaces_final_marker_and_keeps_raw_gateway_response(runtime_services):  # noqa: F811
    """真实图消息归并后只有一条已渲染回复，原供应商输出仍留在账本。"""
    module, context, run = setup_run(runtime_services)
    source = context.sources.import_text("固定材料", "重复任务需要幂等。")
    evidence = context.sources.read_segment(source["segments"][0]["id"], root_id=run["id"])
    original = f"结论[citation:{evidence['id']}]"

    class MarkerProvider:
        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            yield AIMessageChunk(content=original)

    context.gateway.client = MarkerProvider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    state = graph.invoke(
        {"messages": [HumanMessage(run["task"])]},
        config={"configurable": {"thread_id": run["checkpoint_id"]}},
        context=context,
    )
    answers = [message for message in state["messages"] if isinstance(message, AIMessage)]
    assert len(answers) == 1
    assert answers[0].content == f"结论[来源](/api/evidence/{evidence['id']})"
    records = context.gateway.ledger.records(root_id=run["id"])
    assert len(records) == 1
    assert records[0]["response"]["content"] == original
