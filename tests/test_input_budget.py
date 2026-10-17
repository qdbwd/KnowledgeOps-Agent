"""完整输入预算的不变量：必要信息优先，背景可补读，保留最长完整近期后缀。"""

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.harness.context.budget import (
    background_data,
    fit_background,
    with_background,
)
from knowledgeops_agent.harness.context.capacity import ContextCapacityExceeded, ModelCapacity
from knowledgeops_agent.harness.context.management import ContextMiddleware, capacity_history_split

runtime_services = _runtime_services


def profile(**overrides):
    return ModelCapacity(working_window=20000, output_reserve=1000, framing_reserve=100, **overrides)


def measured(messages):
    size = 1000 + sum(len(str(message.content)) for message in messages)
    return {"input_tokens": size, "total_tokens": size + 1100}


def test_retains_longest_suffix_toward_80_percent_including_latest_user_and_tool_group():
    messages = [HumanMessage("历史" * 500, id=f"old-{i}") for i in range(20)]
    messages += [HumanMessage("当前完整要求" * 300, id="current"),
                 AIMessage("", tool_calls=[{"id": "call", "name": "read", "args": {}}]),
                 ToolMessage("真实结果" * 100, tool_call_id="call")]
    prefix, tail = capacity_history_split(messages, measured, profile())
    assert prefix and len(tail) > 8
    assert messages[-3:] == tail[-3:]
    assert measured(tail)["input_tokens"] + 2400 <= 16000
    assert measured([prefix[-1], *tail])["input_tokens"] + 2400 > 16000
    legacy_prefix, legacy_tail = capacity_history_split(
        messages, measured, profile(input_budget_version=0)
    )
    assert len(legacy_prefix) > len(prefix) and len(legacy_tail) < len(tail)


def test_oversized_current_input_stops_before_summary_or_model_call():
    context = SimpleNamespace(gateway=SimpleNamespace(capacity=profile()))
    state = {"messages": [HumanMessage("old"), AIMessage("prior"),
                          HumanMessage("不可丢的当前要求" * 3000)]}
    with pytest.raises(ContextCapacityExceeded, match="未调用摘要"):
        ContextMiddleware()._compact(context, state, state["messages"], measured, strict=True)


def test_optional_background_shrinks_first_keeps_requirements_and_reuses_readable_archive():
    saved = []
    context = SimpleNamespace(
        gateway=SimpleNamespace(capacity=profile(), _redact=lambda value: value), run_id="run",
        jobs=SimpleNamespace(save_artifact=lambda run, kind, text: saved.append(text) or "artifact"),
    )
    message = HumanMessage("", additional_kwargs={"knowledgeops_context_data": True})
    body = {"current_task": "真实当前任务", "user_guidance": [{"id": "original", "text": "旧但有效的要求"}],
            "preferences": [{"id": "p", "value": "简洁"}] * 2,
            "evidence_catalog": [{"id": str(i), "title": "冗长目录" * 300} for i in range(20)]}
    message = with_background(message, body)
    shortened, update = fit_background(context, {}, [message], True, measured)
    data = background_data(shortened[0])
    assert data["current_task"] == body["current_task"]
    assert data["user_guidance"] == body["user_guidance"]
    assert len(data["preferences"]) == 1
    assert len(data["evidence_catalog"]) < 20
    assert data["background_omitted"]["artifact_id"] == "artifact"
    assert len(json.loads(saved[0])["evidence_catalog"]) == 20
    fit_background(context, update, [message], True, measured)
    assert len(saved) == 1
    assert message.content == with_background(message, body).content


def test_model_gateway_component_sum_matches_complete_input(runtime_services):
    _, context, _ = setup_run(runtime_services)
    background = with_background(
        HumanMessage("", additional_kwargs={"knowledgeops_context_data": True}),
        {"current_task": "本次请求", "user_guidance": ["有效修订"],
         "preferences": [{"text": "详略习惯"}], "evidence_catalog": [{"id": "e1"}]},
    )
    messages = [SystemMessage("权限和系统规则"), background,
                HumanMessage("用户问题"), AIMessage("已经回答"), HumanMessage("继续")]
    measured_total = context.gateway.measure(messages)
    components = context.gateway.measure_components(messages)
    inputs = ("system_rules", "required_background", "optional_background",
              "history", "tool_schemas", "protocol_base")
    assert sum(components[key] for key in inputs) == measured_total["input_tokens"]
    assert components["required_background"] > 0
    assert components["optional_background"] > 0
    assert components["history"] > 0
    assert context.gateway.ledger.records(root_id=context.root_id) == []


def test_schema_pressure_unloads_old_optional_definitions_but_preserves_newly_discovered():
    from langchain.agents.middleware.types import ModelResponse
    from langchain_core.tools import StructuredTool

    def external(name, length):
        return StructuredTool(name=name, description="x" * length,
                              args_schema={"type": "object", "properties": {}}, func=lambda: "ok",
                              metadata={"knowledgeops_tool_id": "mcp:service:" + name})
    older, recent = external("old", 12000), external("recent", 100)
    def measure(messages, *, tools=(), **kwargs):
        size = measured(messages)
        schemas = sum(len(tool.description) for tool in tools)
        return {"input_tokens": size["input_tokens"] + schemas,
                "total_tokens": size["total_tokens"] + schemas}
    context = SimpleNamespace(resources={}, gateway=SimpleNamespace(capacity=profile(), measure=measure))
    state = {"messages": [HumanMessage("当前要求" * 2000)],
             "promoted_tools": {"catalog_hash": "catalog", "run_id": "run",
                                "names": ["old", "recent"], "recent": ["recent"]}}
    class Request:
        runtime = SimpleNamespace(context=context)
        messages = state["messages"]
        system_message = None
        model_settings = {}
        tool_choice = None
        tools = [older, recent]
        def override(self, **updates):
            return SimpleNamespace(**updates)
    request = Request()
    request.state = state
    visible = []
    def handler(prepared):
        visible.extend(tool.name for tool in prepared.tools)
        assert prepared.messages == state["messages"]
        return ModelResponse(result=[AIMessage("ok")])
    result = ContextMiddleware().wrap_model_call(request, handler)
    assert visible == ["recent"]
    assert result.command.update["promoted_tools"].value["names"] == ["recent"]
