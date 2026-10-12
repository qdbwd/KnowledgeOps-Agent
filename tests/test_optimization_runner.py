"""评估设计必须固定配对数量、模式条件和请求计数，不消耗真实模型。"""

import json
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.checkpoint.memory import InMemorySaver
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from evaluation.optimization import (
    AuxiliaryExperiment,
    Conversation,
    auxiliary_case,
    auxiliary_policy,
    schedule,
)
from knowledgeops_agent.jobs import RunStopped

runtime_services = _runtime_services


def test_pairing_balances_order_and_keeps_autonomous_observation_separate():
    rows = schedule("auxiliary")
    assert len(rows) == 20
    for key in ("A01", "A02", "A03", "A04"):
        group = [r[1:] for r in rows if r[0] == key]
        assert group == [("off", 1), ("fixed_once", 1), ("fixed_once", 2), ("off", 2), ("auto", 1)]
    # 八类机制保留原I08回归，并新增一份独立材料I09，不掩盖已用于调试的旧失败。
    assert len(schedule("interactive")) == 9


def test_fixed_condition_changes_real_model_request_and_removes_auxiliary_after_use():
    records = []
    context = SimpleNamespace(
        root_id="r", gateway=SimpleNamespace(ledger=SimpleNamespace(records=lambda **_: records))
    )
    request = ModelRequest(
        model=SimpleNamespace(),
        messages=[],
        system_message=SystemMessage("rules"),
        tools=[SimpleNamespace(name="call_model"), SimpleNamespace(name="read_source")],
        runtime=SimpleNamespace(context=context),
        state={},
    )
    policy = AuxiliaryExperiment("fixed_once")
    first = policy.wrap_model_call(request, lambda item: item)
    assert [t.name for t in first.tools] == ["call_model"]
    assert first.tool_choice["function"]["name"] == "call_model"
    assert first.model_settings["extra_body"]["thinking"]["type"] == "disabled"
    records.append({"purpose": "auxiliary"})
    next_request = policy.wrap_model_call(request, lambda item: item)
    assert [t.name for t in next_request.tools] == ["read_source"]
    control = AuxiliaryExperiment("off").wrap_model_call(request, lambda item: item)
    assert control.model_settings == next_request.model_settings


class ParallelAuxiliaryProvider:
    """真实图提出两个并行调用；只替换供应商传输，不替换工具/账本。"""

    def __init__(self):
        self.auxiliary_inputs = []
        self.observed_results = []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        self.auxiliary_inputs.append(messages)
        return AIMessage("唯一辅助结果", response_metadata={"finish_reason": "stop"})

    def stream(self, messages, **kwargs):
        observed = [m for m in messages if isinstance(m, ToolMessage)]
        if not observed:
            yield AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "name": "call_model",
                        "args": json.dumps({"prompt": f"独立核对{i}", "material_refs": []}),
                        "id": f"aux-proposal-{i}",
                        "index": i,
                    }
                    for i in range(2)
                ],
            )
            return
        self.observed_results = observed
        yield AIMessageChunk(content="已保留一次辅助核对结果，另一项按实验限制未执行。")


def test_parallel_auxiliary_proposals_keep_success_and_return_tracked_error(runtime_services):
    module, context, run = setup_run(runtime_services, auxiliary=True, task="核对一次，不保存。")
    provider = ParallelAuxiliaryProvider()
    context.gateway.client = provider
    with auxiliary_policy("fixed_once"):
        graph = module.build_agent(context, checkpointer=InMemorySaver())
    state = graph.invoke(
        {"messages": [HumanMessage(run["task"])]},
        config={"configurable": {"thread_id": run["checkpoint_id"]}},
        context=context,
    )
    assert state["messages"][-1].content == "已保留一次辅助核对结果，另一项按实验限制未执行。"
    results = provider.observed_results
    assert {m.tool_call_id for m in results} == {"aux-proposal-0", "aux-proposal-1"}
    success = [m for m in results if m.status == "success"]
    errors = [m for m in results if m.status == "error"]
    assert len(success) == len(errors) == 1
    assert json.loads(errors[0].content)["error"] == "fixed_once实验只允许一次真实辅助请求"
    result = json.loads(success[0].content)
    assert result["preview"] == "唯一辅助结果"
    artifact = json.loads(context.jobs.artifact_content(result["artifact_id"], root_id=run["id"]))
    assert artifact["text"] == "唯一辅助结果"
    assert artifact["tool_call_id"] == success[0].tool_call_id
    assert len(provider.auxiliary_inputs) == 1
    records = context.gateway.ledger.records(root_id=run["id"])
    assert [r["purpose"] for r in records] == ["agent", "auxiliary", "agent"]
    assert context.jobs.get_run(run["id"])["tool_calls"] == 2
    events = context.jobs.events(run["id"], limit=200)
    starts = [e["data"]["tool_call_id"] for e in events if e["kind"] == "tool_started"]
    finishes = {
        e["data"]["tool_call_id"]: e["data"] for e in events if e["kind"] == "tool_finished"
    }
    assert set(starts) == set(finishes) == {"aux-proposal-0", "aux-proposal-1"}
    assert finishes[errors[0].tool_call_id]["status"] == "error"
    assert finishes[success[0].tool_call_id]["status"] == "success"


def test_duplicate_auxiliary_proposal_cannot_bypass_tool_budget(runtime_services):
    module, context, run = setup_run(
        runtime_services, auxiliary=True, budgets={"tool_calls": 1}, task="核对一次，不保存。"
    )
    provider = ParallelAuxiliaryProvider()
    context.gateway.client = provider
    with auxiliary_policy("fixed_once"):
        graph = module.build_agent(context, checkpointer=InMemorySaver())
    with pytest.raises(RunStopped, match="tool budget"):
        graph.invoke(
            {"messages": [HumanMessage(run["task"])]},
            config={"configurable": {"thread_id": run["checkpoint_id"]}},
            context=context,
        )
    assert context.jobs.get_run(run["id"])["tool_calls"] == 1
    assert len(provider.auxiliary_inputs) <= 1
    assert not provider.observed_results


def test_fixed_experiment_does_not_force_hidden_tool_when_finalizing(runtime_services):
    module, context, run = setup_run(
        runtime_services, auxiliary=True, budgets={"model_requests": 2}, task="只说明现有结果。"
    )

    class FinalProvider:
        def stream(self, messages, **kwargs):
            assert not kwargs.get("tools")
            assert kwargs.get("tool_choice") is None
            yield AIMessageChunk(content="当前没有辅助结果。")

    context.gateway.client = FinalProvider()
    with auxiliary_policy("fixed_once"):
        graph = module.build_agent(context, checkpointer=InMemorySaver())
    state = graph.invoke(
        {"messages": [HumanMessage(run["task"])]},
        config={"configurable": {"thread_id": run["checkpoint_id"]}},
        context=context,
    )
    assert state["messages"][-1].content == "当前没有辅助结果。"
    assert [r["purpose"] for r in context.gateway.ledger.records(root_id=run["id"])] == ["agent"]
    assert context.jobs.get_run(run["id"])["tool_calls"] == 0


def test_auxiliary_cases_store_explicit_task_switch(runtime_services, monkeypatch):
    jobs, knowledge, sources, _ = runtime_services
    services = SimpleNamespace(jobs=jobs, knowledge=knowledge, sources=sources)
    submitted = []

    def capture_enqueued_run(self, run):
        submitted.append(run["id"])
        return {"run": run, "models": []}

    # 在worker边界停止；真实enqueue必须保存各实验模式的选择，不能触发模型。
    monkeypatch.setattr(Conversation, "execute", capture_enqueued_run)
    case = {"title": "开关夹具", "material": "固定材料。", "task": "核对。", "expected": []}
    for mode in ("off", "fixed_once", "auto"):
        auxiliary_case(services, case, mode)
    assert [jobs.get_run(run_id)["input"]["auxiliary"] for run_id in submitted] == [
        False,
        True,
        True,
    ]
