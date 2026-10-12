"""压缩只改变模型工作上下文，原始输入、证据与权限事实必须保留。"""

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

runtime_services = _runtime_services


def test_recent_history_retention_uses_capacity_not_an_eight_message_ceiling():
    from knowledgeops_agent.context_management import capacity_history_split
    from knowledgeops_agent.request_capacity import ModelCapacity

    capacity = ModelCapacity(working_window=100000)
    messages = [HumanMessage("较早资料" * 20000, id="old")]
    messages += [HumanMessage(f"最新约束{index}", id=str(index)) for index in range(24)]

    def measure(view):
        tokens = 1000 + sum(len(message.content) for message in view)
        return {"input_tokens": tokens, "total_tokens": tokens + 8192 + 4096}

    prefix, tail = capacity_history_split(messages, measure, capacity)
    assert prefix == messages[:1]
    assert tail == messages[1:]


def test_long_history_is_summarized_through_ledger_with_originals_preserved(runtime_services):
    module, context, run = setup_run(runtime_services)
    # 小窗口强制覆盖真实分词后的摘要分支，不生成百万token的慢测试材料。
    context.gateway.capacity = context.gateway.capacity.model_copy(update={"working_window": 32768})

    class ContextProvider:
        def invoke(self, messages, **kwargs):
            assert "早期要求" in str(messages)
            return AIMessage(content="早期要求：保持段落与来源，禁止自行发布。")

        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            assert "早期要求：保持段落与来源" in str(messages)
            # 预算和草稿状态仍由运行控制注入，不能使用摘要产生的权限。
            assert '"publication_mode": "preview"' in str(messages)
            yield AIMessageChunk(content="记得早期要求；仅起草。")

    context.gateway.client = ContextProvider()
    messages = [HumanMessage("早期要求：保持段落与来源，禁止自行发布。" + "旧讨论" * 20000)]
    for index in range(10):
        messages += [HumanMessage(f"讨论 {index}"), AIMessage("已讨论")]
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    result = graph.invoke(
        {"messages": messages}, {"configurable": {"thread_id": run["id"]}}, context=context
    )
    # 原始State不删除，只有发给模型的工作视图被压缩；检查点仍可恢复原文。
    assert result["messages"][0].content == messages[0].content
    assert "早期要求" in result["working_summary"]
    records = context.gateway.ledger.records(root_id=run["id"])
    assert records[-1]["purpose"] == "agent"
    assert all(row["purpose"] == "context_summary" for row in records[:-1])
    assert len(str(records[-1]["request"]["messages"])) < len(messages[0].content)
    artifacts = context.jobs.artifacts(run["id"])
    original = next(a for a in artifacts if a["kind"] == "context_original")
    restored = context.jobs.read_artifact(original["id"], root_id=run["id"], length=12000)
    assert "早期要求" in restored["text"] and restored["next_start"] is not None


def test_large_tool_result_is_retrievable_but_not_all_repeated_in_context(runtime_services):
    module, context, run = setup_run(runtime_services)
    middleware = module.RuntimeControlMiddleware()
    content = "完整工具结果" * 4000
    request = SimpleNamespace(
        runtime=SimpleNamespace(context=context),
        state={},
        tool_call={
            "id": "long-call",
            "name": "inspect_material",
            "args": {},
        },
    )
    result = middleware.wrap_tool_call(
        request, lambda _: ToolMessage(content=content, tool_call_id="long-call")
    )
    info = json.loads(result.content)
    assert info["truncated"] and len(result.content) < 6000
    page = context.jobs.read_artifact(info["artifact_id"], root_id=run["id"])
    assert page["text"] == content[:6000]
    assert page["next_start"] == 6000
    event = next(item for item in context.jobs.events(run["id"]) if item["kind"] == "tool_finished")
    assert len(json.dumps(event["data"], ensure_ascii=False)) < 6000
    assert info["artifact_id"] in json.dumps(event["data"])


def test_tool_observation_and_event_redact_known_secrets(runtime_services):
    module, context, run = setup_run(runtime_services)
    secret = "test-secret-only-in-memory"
    context.gateway.sensitive_values = [secret]
    request = SimpleNamespace(
        runtime=SimpleNamespace(context=context),
        state={},
        tool_call={
            "id": "secret-call",
            "name": "inspect_material",
            "args": {"extra": secret},
        },
    )
    result = module.RuntimeControlMiddleware().wrap_tool_call(
        request, lambda _: ToolMessage(content=secret, tool_call_id="secret-call")
    )
    assert secret not in result.content
    assert secret not in json.dumps(context.jobs.events(run["id"]), ensure_ascii=False)


def test_context_compaction_keeps_tool_calls_and_results_together(runtime_services):
    module, context, _ = setup_run(runtime_services)
    from knowledgeops_agent.context_management import split_history

    messages = [HumanMessage("原始任务"), AIMessage("较早输出")]
    messages += [
        AIMessage(content="", tool_calls=[{"name": "read_source", "args": {}, "id": "read"}])
    ]
    messages += [ToolMessage(content="读取结果", tool_call_id="read")]
    messages += [HumanMessage("继续"), AIMessage("好的")]
    prefix, tail = split_history(messages, keep=3)
    assert prefix == messages[:2]
    assert tail[0].tool_calls[0]["id"] == "read"
    assert tail[1].tool_call_id == "read"


def test_long_recent_messages_are_compacted_by_capacity_not_fixed_count(runtime_services):
    """真实I04暴露：最近八条本身超限时，不能反复付费只摘要更早的小前缀。"""
    module, context, run = setup_run(runtime_services)
    context.gateway.capacity = context.gateway.capacity.model_copy(update={"working_window": 32768})

    class LongRecentProvider:
        def invoke(self, messages, **kwargs):
            return AIMessage("早期要求：保留Q17例子和原文位置，超时先查结果；当前只读。")

        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            assert "最新问题" in str(messages)
            assert "Q17" in str(messages)
            yield AIMessageChunk(content="依照原约束回答最新问题。")

    context.gateway.client = LongRecentProvider()
    messages = [HumanMessage("早期要求：保留Q17例子和原文位置，超时先查结果。")]
    for index in range(5):
        messages += [HumanMessage(f"旧材料{index}:" + "0123456789" * 4000), AIMessage("收到")]
    messages += [HumanMessage("最新问题：按照早期约束解释，保持只读。")]
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    result = graph.invoke(
        {"messages": messages}, {"configurable": {"thread_id": run["id"]}}, context=context
    )
    assert len(result["messages"]) == len(messages) + 1
    records = context.gateway.ledger.records(root_id=run["id"])
    assert records[-1]["purpose"] == "agent"
    assert (
        records[-1]["request"]["capacity"]["total_tokens"]
        <= context.gateway.capacity.working_window
    )
    assert any(e["kind"] == "context_compacted" for e in context.jobs.events(run["id"]))


@pytest.mark.parametrize("failure", ["empty", "truncated", "network"])
def test_invalid_summary_does_not_replace_history(runtime_services, failure):
    from knowledgeops_agent.request_capacity import ContextCapacityExceeded

    module, context, run = setup_run(runtime_services)
    context.gateway.capacity = context.gateway.capacity.model_copy(update={"working_window": 32768})

    class BrokenSummary:
        def invoke(self, messages, **kwargs):
            if failure == "network":
                raise ConnectionError("summary unavailable")
            return AIMessage(
                "" if failure == "empty" else "incomplete",
                response_metadata={"finish_reason": "length" if failure == "truncated" else "stop"},
            )

        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            pytest.fail("oversize request must not reach the main provider")

    context.gateway.client = BrokenSummary()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": run["id"]}}
    original = "不可丢失的历史" * 30000
    messages = [HumanMessage(original), *[HumanMessage("后续") for _ in range(10)]]
    with pytest.raises(ContextCapacityExceeded):
        graph.invoke({"messages": messages}, config, context=context)
    state = graph.get_state(config).values
    assert state["messages"][0].content == original
    assert not state.get("working_summary")
    assert not any(a["kind"] == "working_summary" for a in context.jobs.artifacts(run["id"]))
    assert any(a["kind"] == "context_original" for a in context.jobs.artifacts(run["id"]))


def test_large_system_rules_are_checked_even_with_short_history(runtime_services, monkeypatch):
    from knowledgeops_agent.request_capacity import ContextCapacityExceeded

    module, context, run = setup_run(runtime_services)
    context.gateway.capacity = context.gateway.capacity.model_copy(update={"working_window": 32768})
    monkeypatch.setattr(module, "SYSTEM_PROMPT", "必需规则" * 20000)
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    with pytest.raises(ContextCapacityExceeded):
        graph.invoke(
            {"messages": [HumanMessage("简短任务")]},
            {"configurable": {"thread_id": run["id"]}},
            context=context,
        )
    assert context.gateway.ledger.records(root_id=run["id"]) == []


@pytest.mark.parametrize("status", ["success", "error"])
def test_offloaded_tool_result_exposes_exact_unicode_preview_and_resume_offset(
    runtime_services, status
):
    """外置回执必须能定位真实字符范围，且不能覆盖原工具状态或材料状态。"""
    module, context, run = setup_run(runtime_services)
    content = json.dumps(
        {
            "status": "ready",
            "segments": [{"position": index, "label": "原文😀"} for index in range(700)],
        },
        ensure_ascii=False,
    )
    request = SimpleNamespace(
        runtime=SimpleNamespace(context=context),
        state={},
        tool_call={"id": "unicode-index", "name": "inspect_material", "args": {}},
    )
    original = ToolMessage(
        content=content, tool_call_id="unicode-index", name="inspect_material", status=status
    )
    result = module.RuntimeControlMiddleware().wrap_tool_call(request, lambda _: original)
    receipt = json.loads(result.content)

    assert receipt["delivery"] == "tool_output_artifact"
    assert receipt["tool_name"] == "inspect_material"
    assert receipt["offset_unit"] == "unicode_code_points"
    assert receipt["preview_start"] == 0
    assert receipt["preview_end"] == len(receipt["preview"]) < len(content)
    assert receipt["preview"] == content[: receipt["preview_end"]]
    assert receipt["total_length"] == len(content)
    assert receipt["next_start"] == receipt["preview_end"]
    assert receipt["truncated"] is True
    assert result.status == status
    assert result.tool_call_id == original.tool_call_id
    assert result.name == original.name

    # 直接使用回执偏移续读应无重叠或漏字；只需一页，不要求读完整份材料。
    page = context.jobs.read_artifact(
        receipt["artifact_id"], root_id=run["id"], start=receipt["next_start"]
    )
    assert page["kind"] == "tool_result"
    assert page["start"] == receipt["preview_end"]
    assert receipt["preview"] + page["text"] == content[: page["end"]]
    assert page["next_start"] is not None
    stored = context.jobs.artifact_content(receipt["artifact_id"], root_id=run["id"])
    assert stored == content
    assert json.loads(stored)["status"] == "ready"
