"""复现真实 I02 的 DSML 包装；拒绝泄漏，不执行正文中的调用。"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langgraph.checkpoint.memory import InMemorySaver
from test_api_worker import services as _services
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.response_validation import FinalAnswerValidation

services = _services
runtime_services = _runtime_services

# b4280fc8-77f9-425c-8d07-811f792aae4d/I02-interactive-1.json：
# turns[2].models ordinal=1368，tool_calls=[]、finish_reason=stop。
# 保留真实协议字符、空格和工具名；仅把材料 UUID 换成无关联的合成值。
ACTUAL_DSML = """<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="inspect_material">
<｜｜DSML｜｜ parameter name="source_id" string="true">00000000-0000-4000-8000-000000000001</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>"""


def test_actual_dsml_is_rejected_without_tool_execution_or_billable_repair(runtime_services):
    _, context, _ = setup_run(runtime_services)
    with pytest.raises(RuntimeError, match="结构化工具调用"):
        FinalAnswerValidation().after_model(
            {"messages": [AIMessage(ACTUAL_DSML)]}, SimpleNamespace(context=context)
        )
    events = context.jobs.events(context.run_id)
    assert any(event["kind"] == "model_protocol_error" for event in events)
    assert not any(event["kind"] == "tool_started" for event in events)
    assert context.gateway.ledger.records() == []
    assert context.knowledge.list_drafts(root_id=context.root_id) == []


@pytest.mark.parametrize(
    "content",
    [f"```xml\n{ACTUAL_DSML}\n```", f"`{ACTUAL_DSML}`", f"协议示例如下：\n{ACTUAL_DSML}"],
)
def test_actual_dsml_in_documentation_remains_a_valid_answer(runtime_services, content):
    _, context, _ = setup_run(runtime_services)
    assert FinalAnswerValidation().after_model(
        {"messages": [AIMessage(content)]}, SimpleNamespace(context=context)
    ) == {"final_response_feedback": []}


def test_actual_dsml_in_a_legacy_checkpoint_is_not_delivered_as_an_answer(runtime_services):
    from knowledgeops_agent.worker import _outcome

    _, context, _ = setup_run(runtime_services)
    outcome = _outcome(context, {"messages": [AIMessage(ACTUAL_DSML)]})
    assert outcome["kind"] == "partial"
    assert outcome["answer"] == ""


def test_actual_dsml_worker_fails_and_keeps_raw_response_without_another_request(
    services, monkeypatch
):
    from knowledgeops_agent import worker

    class Provider:
        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            yield AIMessageChunk(content=ACTUAL_DSML, response_metadata={"finish_reason": "stop"})

    services.gateway.enabled = True
    services.gateway.client = Provider()
    monkeypatch.setattr(worker, "ensure_publication_intent", lambda context: {})
    conversation = services.jobs.create_conversation("真实协议形态回归")
    run = services.jobs.enqueue(conversation["id"], "读取材料", request_key="actual-dsml-leak")
    result = worker.execute_run(
        run["id"],
        run["generation"],
        services=services,
        worker_id="actual-dsml-worker",
        checkpointer=InMemorySaver(),
    )
    assert result["status"] == "failed"
    assert result["outcome"]["kind"] == "partial"
    assert result["outcome"]["answer"] == ""
    assert "结构化工具调用" in result["stop_reason"]
    records = services.gateway.ledger.records(root_id=run["root_id"])
    assert len(records) == 1
    assert records[0]["response"]["content"] == ACTUAL_DSML
    assert records[0]["response"]["tool_calls"] == []
    assert not any(event["kind"] == "tool_started" for event in services.jobs.events(run["id"]))
    assert not services.knowledge.list_drafts(root_id=run["root_id"])
