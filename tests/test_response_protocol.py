"""协议泄漏必须明确失败，不能把文本中的调用标记执行或交付为成功。"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langgraph.checkpoint.memory import InMemorySaver
from test_api_worker import services as _services
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.harness.models.validation import FinalAnswerValidation

services = _services
runtime_services = _runtime_services
RAW = '<｜DSML｜tool_calls><｜DSML｜invoke name="prepare_changes">payload</｜DSML｜invoke></｜DSML｜tool_calls>'


def test_unexecuted_protocol_text_is_not_a_final_answer_or_a_billable_repair(runtime_services):
    _, context, _ = setup_run(runtime_services)
    with pytest.raises(RuntimeError, match="结构化工具调用"):
        FinalAnswerValidation().after_model(
            {"messages": [AIMessage(RAW)]}, SimpleNamespace(context=context)
        )
    assert context.gateway.ledger.records() == []
    assert context.knowledge.list_drafts(root_id=context.root_id) == []
    assert any(e["kind"] == "model_protocol_error" for e in context.jobs.events(context.run_id))


@pytest.mark.parametrize("content", [f"```xml\n{RAW}\n```", f"`{RAW}`", "DSML 是一种表示格式。"])
def test_documented_protocol_examples_remain_valid_answers(runtime_services, content):
    _, context, _ = setup_run(runtime_services)
    assert FinalAnswerValidation().after_model(
        {"messages": [AIMessage(content)]}, SimpleNamespace(context=context)
    ) == {"final_response_feedback": []}


def test_outcome_filters_checkpointed_raw_protocol_even_without_feedback(runtime_services):
    from knowledgeops_agent.app.runs.worker import _outcome

    _, context, _ = setup_run(runtime_services)
    outcome = _outcome(context, {"messages": [AIMessage(RAW)]})
    assert outcome["kind"] == "partial"
    assert outcome["answer"] == ""


def test_worker_records_failed_run_and_preserves_raw_provider_response(services, monkeypatch):
    from knowledgeops_agent.app.runs import worker as worker

    class Provider:
        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            yield AIMessageChunk(content=RAW)

    services.gateway.enabled = True
    services.gateway.client = Provider()
    monkeypatch.setattr(worker, "ensure_publication_intent", lambda context: {})
    conversation = services.jobs.create_conversation("协议边界")
    run = services.jobs.enqueue(conversation["id"], "读取材料", request_key="protocol-leak")
    result = worker.execute_run(
        run["id"],
        run["generation"],
        services=services,
        worker_id="protocol-worker",
        checkpointer=InMemorySaver(),
    )
    assert result["status"] == "failed"
    assert result["outcome"]["kind"] == "partial"
    assert result["outcome"]["answer"] == ""
    assert "结构化工具调用" in result["stop_reason"]
    records = services.gateway.ledger.records(root_id=run["root_id"])
    assert len(records) == 1
    assert records[0]["response"]["content"] == RAW
    assert not services.knowledge.list_drafts(root_id=run["root_id"])
