"""任务连续性验证：原话定位、可达范围和模型笔记不等于新授权。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

runtime_services = _runtime_services


def enable_continuity(context):
    from knowledgeops_agent.harness.tools.policy import ToolPolicyService

    service = ToolPolicyService(context.jobs.sessions)
    policy = service.get()
    service.update(
        expected_revision=policy["revision"],
        enabled={
            "builtin:history_search": True,
            "builtin:history_read": True,
            "builtin:task_note": True,
        },
    )


def test_note_schema_exposes_constraints_that_real_model_cannot_guess():
    """真实联调的中文key被拒绝；参数约束必须出现在模型实际收到的Schema中。"""
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from knowledgeops_agent.harness.context.continuity import task_note

    properties = convert_to_openai_tool(task_note)["function"]["parameters"]["properties"]
    assert properties["key"]["pattern"] == "^[a-zA-Z0-9_-]{1,40}$"
    assert "xingzhou" in properties["key"]["description"]
    assert properties["content"]["maxLength"] == 750
    sources = next(item for item in properties["source_ids"]["anyOf"] if item["type"] == "array")
    assert sources["maxItems"] == 4
    assert sources["items"]["pattern"] == "^r[a-f0-9]{32}$"


def test_real_graph_identifies_bad_note_key_and_accepts_corrected_call(runtime_services):
    """回放真实失败参数，验证错误指出key后可以修正，笔记进入下轮输入和快照。"""
    from langchain_core.messages import AIMessageChunk, ToolMessage

    from knowledgeops_agent.harness.context.continuation import from_state

    module, context, run = setup_run(runtime_services)
    enable_continuity(context)

    class Provider:
        step = 0

        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            step = self.step
            self.step += 1
            if step == 1:
                feedback = next(m for m in reversed(messages) if isinstance(m, ToolMessage))
                assert feedback.status == "error"
                assert "key" in feedback.content
                assert "[a-zA-Z0-9_-]" in feedback.content
            if step < 2:
                yield AIMessageChunk(
                    "",
                    tool_calls=[
                        {
                            "name": "task_note",
                            "id": f"note{step}",
                            "args": {
                                "key": "星舟工作稿约定" if step == 0 else "xingzhou_agreement",
                                "content": "先核对版本差异，再执行已授权操作。",
                            },
                        }
                    ],
                )
                return
            background = next(
                m for m in messages if m.additional_kwargs.get("knowledgeops_context_data")
            )
            assert "xingzhou_agreement" in background.content
            assert "先核对版本差异" in background.content
            yield AIMessageChunk("笔记已保存。")

    context.gateway.client = Provider()
    result = module.build_agent(context, checkpointer=InMemorySaver()).invoke(
        {"messages": [HumanMessage("保存会话工作笔记", id="note-request")]},
        {"configurable": {"thread_id": run["id"]}},
        context=context,
    )
    assert list(result["task_notes"]) == ["xingzhou_agreement"]
    assert from_state(context, result)["task_notes"] == result["task_notes"]


def test_real_graph_search_read_note_survive_next_run_without_inheriting_authority(
    runtime_services,
):
    import json

    from langchain_core.messages import AIMessageChunk, ToolMessage

    from knowledgeops_agent.harness.context.continuation import from_state

    module, context, run = setup_run(runtime_services)
    enable_continuity(context)

    class Provider:
        step = 0
        source = None

        def bind_tools(self, tools):
            assert {"history_search", "history_read", "task_note"} <= {t.name for t in tools}
            return self

        def stream(self, messages, **kwargs):
            step = self.step
            self.step += 1
            if step == 0:
                name, args = "history_search", {"query": "回执", "role": "user"}
            elif step == 1:
                observation = json.loads(
                    next(m.content for m in reversed(messages) if isinstance(m, ToolMessage))
                )
                self.source = observation["results"][0]["id"]
                name, args = "history_read", {"source_id": self.source}
            elif step == 2:
                assert "先查回执" in messages[-1].content
                name, args = (
                    "task_note",
                    {"key": "receipt", "content": "先查回执", "source_ids": [self.source]},
                )
            else:
                background = next(
                    m for m in messages if m.additional_kwargs.get("knowledgeops_context_data")
                )
                assert "model_report" in background.content
                assert "先查回执" in background.content
                yield AIMessageChunk("已记录任务要求。")
                return
            yield AIMessageChunk("", tool_calls=[{"name": name, "args": args, "id": f"tool{step}"}])

    context.gateway.client = Provider()
    saver = InMemorySaver()
    graph = module.build_agent(context, checkpointer=saver)
    state = graph.invoke(
        {"messages": [HumanMessage("写入超时先查回执", id="user-origin")]},
        {"configurable": {"thread_id": run["id"]}},
        context=context,
    )
    assert state["task_notes"]["receipt"]["source_ids"]
    context.jobs.finish(run["id"], context.worker_id, continuation=from_state(context, state))
    next_run = context.jobs.enqueue(
        run["conversation_id"], "继续说明", request_key="next-turn", publication_mode="preview"
    )
    context.jobs.claim(context.worker_id, run_id=next_run["id"], generation=1)
    newer = replace(context, run_id=next_run["id"], root_id=next_run["id"], resources=None)
    next_graph = module.build_agent(newer, checkpointer=saver)
    continued = next_graph.invoke(
        {"messages": module.initial_messages(newer)},
        {"configurable": {"thread_id": next_run["id"]}},
        context=newer,
    )
    assert (
        continued["task_notes"]["receipt"]["source_ids"]
        == state["task_notes"]["receipt"]["source_ids"]
    )
    assert newer.jobs.get_run(newer.run_id)["input"]["publication_mode"] == "preview"


def test_role_filter_precedes_result_limit(runtime_services):
    from knowledgeops_agent.harness.context.history import capture_history, lookup_history

    _, context, _ = setup_run(runtime_services)
    messages = [HumanMessage("回执只能查询不要重做", id="original")]
    messages += [AIMessage(f"回执解释{index}", id=f"ai{index}") for index in range(20)]
    archived = capture_history(context, {}, messages)
    for state in ({"messages": messages}, {"task_history": archived}):
        results = lookup_history(context, state, query="回执", role="human")["results"]
        assert len(results) == 1 and results[0]["message_id"] == "original"


def test_old_snapshot_original_artifact_is_indexed_without_reintroducing_summary_as_user(
    runtime_services,
):
    import json

    from langchain_core.messages import message_to_dict

    from knowledgeops_agent.harness.context.history import bootstrap_history, lookup_history

    _, context, _ = setup_run(runtime_services)
    original = HumanMessage("原始约束孤岛", id="original")
    artifact_id = context.jobs.save_artifact(
        context.run_id, "context_original", json.dumps([original.model_dump()])
    )
    content = {
        "messages": [message_to_dict(HumanMessage("助手摘要断言", id="summary-old"))],
        "artifact_ids": [artifact_id],
    }
    history = bootstrap_history(context, content)
    assert lookup_history(context, {"task_history": history}, query="孤岛")["results"]
    assert lookup_history(context, {"task_history": history}, query="断言")["results"] == []


@pytest.mark.parametrize("failure", ["malformed", "missing", "denied", "missing_metadata"])
def test_unreadable_legacy_history_keeps_available_context_and_reports_gap(
    runtime_services, monkeypatch, failure
):
    from langchain_core.messages import message_to_dict

    from knowledgeops_agent.harness.context.history import bootstrap_history, lookup_history

    _, context, _ = setup_run(runtime_services)
    artifact = context.jobs.save_artifact(context.run_id, "context_original", "not-json")
    if failure == "missing_metadata":
        artifact = "00000000-0000-0000-0000-000000000001"
    elif failure != "malformed":

        def unavailable(*args, **kwargs):
            raise (KeyError if failure == "missing" else PermissionError)("unavailable")

        monkeypatch.setattr(context.jobs, "artifact_content", unavailable)
    history = bootstrap_history(
        context,
        {
            "messages": [message_to_dict(HumanMessage("仍有可读的原话孤岛", id="available"))],
            "artifact_ids": [artifact],
        },
    )
    assert history["status"] == "unavailable"
    result = lookup_history(context, {"task_history": history}, query="孤岛")
    assert result["results"][0]["message_id"] == "available"
    assert result["status"] == "unavailable"
    event = next(
        e for e in context.jobs.events(context.run_id) if e["kind"] == "history_unavailable"
    )
    assert event["data"]["artifact_id"] == artifact


def test_compaction_then_new_turn_can_read_original_omitted_from_summary(runtime_services):
    """摘要故意漏掉原话；在真实图和新run中通过索引恢复，不能靠旧State碰巧命中。"""
    import json

    from langchain_core.messages import AIMessageChunk, ToolMessage

    from knowledgeops_agent.harness.context.continuation import from_state

    module, context, run = setup_run(runtime_services)
    enable_continuity(context)
    context.gateway.capacity = context.gateway.capacity.model_copy(update={"working_window": 32768})
    original = "孤岛原话：执行之前需要先展示影响。"

    class Provider:
        recovering = False
        step = 0

        def invoke(self, messages, **kwargs):
            return AIMessage("之前进行过多轮讨论；当前只读，需继续核对原始要求。")

        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            if not self.recovering:
                assert original not in str(messages)
                yield AIMessageChunk("暂时保留讨论。")
                return
            step = self.step
            self.step += 1
            if step == 0:
                assert original not in str(messages)
                name, args = "history_search", {"query": "孤岛", "role": "user"}
            elif step == 1:
                found = json.loads(
                    next(m.content for m in reversed(messages) if isinstance(m, ToolMessage))
                )
                assert found["results"][0]["message_id"] == "old-rule"
                name, args = "history_read", {"source_id": found["results"][0]["id"]}
            else:
                result = json.loads(
                    next(m.content for m in reversed(messages) if isinstance(m, ToolMessage))
                )
                assert result["text"].startswith(original)
                assert result["role"] == "human"
                yield AIMessageChunk("已经读取早期原话。")
                return
            yield AIMessageChunk(
                "", tool_calls=[{"name": name, "args": args, "id": f"recover{step}"}]
            )

    provider = Provider()
    context.gateway.client = provider
    saver = InMemorySaver()
    graph = module.build_agent(context, checkpointer=saver)
    messages = [HumanMessage(original + "旧讨论" * 20000, id="old-rule")]
    messages += [HumanMessage(f"后续轮次{index}", id=f"turn{index}") for index in range(12)]
    state = graph.invoke(
        {"messages": messages}, {"configurable": {"thread_id": run["id"]}}, context=context
    )
    assert state["context_compaction"]
    snapshot = from_state(context, state)
    assert original not in str(snapshot["messages"])
    context.jobs.finish(run["id"], context.worker_id, continuation=snapshot)
    next_run = context.jobs.enqueue(
        run["conversation_id"],
        "找回最早的要求",
        request_key="recover-next",
        publication_mode="preview",
    )
    context.jobs.claim(context.worker_id, run_id=next_run["id"], generation=1)
    newer = replace(context, run_id=next_run["id"], root_id=next_run["id"], resources=None)
    provider.recovering = True
    next_graph = module.build_agent(newer, checkpointer=saver)
    result = next_graph.invoke(
        {"messages": module.initial_messages(newer)},
        {"configurable": {"thread_id": next_run["id"]}},
        context=newer,
    )
    assert result["messages"][-1].content == "已经读取早期原话。"


def test_compacted_history_can_recover_exact_chinese_source_and_paginate(runtime_services):
    from knowledgeops_agent.harness.context.history import capture_history, lookup_history

    _, context, _ = setup_run(runtime_services)
    text = "订单写入超时，先查回执。" + "早期详细解释。" * 1000 + "末尾定位词孤岛"
    messages = [HumanMessage(text, id="user-rule")]
    history = capture_history(context, {}, messages)
    state = {"messages": [HumanMessage("摘要未包含原话")], "task_history": history}
    found = lookup_history(context, state, query="回执", role="human")
    assert len(found["results"]) == 1
    source_id = found["results"][0]["id"]
    assert lookup_history(context, state, query="孤岛")["results"][0]["id"] == source_id
    from knowledgeops_agent.harness.context.continuity import history_read

    runtime = SimpleNamespace(context=context, state=state)
    page = history_read.func(source_id=source_id, offset=4000, runtime=runtime)
    assert page["text"] == text[4000:8000]
    assert page["total_length"] == len(text)
    assert page["truncated"] is False


def test_archive_reachability_is_checkpoint_and_scope_bound(runtime_services):
    from knowledgeops_agent.harness.context.history import capture_history, lookup_history

    _, context, _ = setup_run(runtime_services)
    first = capture_history(context, {}, [HumanMessage("原约束", id="first")])
    old = {"task_history": first, "messages": []}
    future = capture_history(context, old, [HumanMessage("将来新要求", id="future")])
    assert lookup_history(context, old, query="将来")["results"] == []
    assert lookup_history(context, {"task_history": future}, query="将来")["results"]
    child = context.jobs.start_child(context.root_id, "子任务", operation_key="scope-child")
    foreign = replace(context, run_id=child["id"])
    assert lookup_history(foreign, old, query="原约束")["results"] == []
    assert lookup_history(foreign, old, query="原约束")["status"] == "scope_unavailable"


def test_capture_is_idempotent_and_source_allowlist_omits_reasoning(runtime_services):
    from knowledgeops_agent.harness.context.history import capture_history, lookup_history

    _, context, _ = setup_run(runtime_services)
    visible = AIMessage(
        [
            {"type": "text", "text": "真实可见答复"},
            {"type": "reasoning", "text": "不可检索思考内容"},
        ],
        id="ai",
        additional_kwargs={"reasoning_content": "另一条隐藏内容"},
    )
    injected = HumanMessage(
        "运行时注入不应归档", id="injected", additional_kwargs={"knowledgeops_context_data": True}
    )
    messages = [visible, injected]
    first = capture_history(context, {}, messages)
    second = capture_history(context, {"task_history": first}, messages)
    assert first == second
    state = {"task_history": second}
    assert lookup_history(context, state, query="思考")["results"] == []
    assert lookup_history(context, state, query="注入")["results"] == []
    assert lookup_history(context, state, query="答复")["results"][0]["text"] == "真实可见答复"


def test_task_note_checks_sources_and_supports_deletion_without_granting_authority(
    runtime_services,
):
    from knowledgeops_agent.harness.context.continuity import task_note
    from knowledgeops_agent.harness.context.history import lookup_history
    from knowledgeops_agent.vendor.deerflow.task_continuity_state import merge_task_notes

    _, context, _ = setup_run(runtime_services)
    state = {"messages": [HumanMessage("先查回执，再决定后续", id="requirement")]}
    runtime = SimpleNamespace(context=context, state=state, tool_call_id="note")
    source = lookup_history(context, state, query="回执")["results"][0]["id"]
    bad = task_note.func(
        key="receipt", content="模型笔记", source_ids=["r" + "0" * 32], runtime=runtime
    )
    assert bad["error"] == "source_unavailable"
    command = task_note.func(
        key="receipt", content="先查回执", source_ids=[source], runtime=runtime
    )
    notes = merge_task_notes({}, command.update["task_notes"])
    assert notes["receipt"]["authority"] == "model_report"
    runtime.state = {**state, "task_notes": notes}
    deleted = task_note.func(key="receipt", content="", source_ids=[], runtime=runtime)
    assert merge_task_notes(notes, deleted.update["task_notes"]) == {}


def test_parallel_notes_do_not_evict_existing_keys(runtime_services):
    from knowledgeops_agent.harness.context.continuity import task_note

    _, context, _ = setup_run(runtime_services)
    notes = {f"n{i}": {"content": str(i), "source_ids": []} for i in range(7)}
    calls = [
        {"name": "task_note", "id": f"call{i}", "args": {"key": f"new{i}", "content": "新笔记"}}
        for i in range(2)
    ]
    state = {"task_notes": notes, "messages": [AIMessage("", tool_calls=calls)]}
    runtime = SimpleNamespace(context=context, state=state, tool_call_id="call1")
    result = task_note.func(key="new1", content="新笔记", source_ids=[], runtime=runtime)
    assert result["error"] == "note_capacity"


def test_context_data_is_ephemeral_separate_from_control_rules_and_counted(runtime_services):
    from langchain_core.messages import AIMessageChunk

    module, context, run = setup_run(runtime_services)
    captured = []

    class Provider:
        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            captured.extend(messages)
            yield AIMessageChunk("答复")

    context.gateway.client = Provider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    result = graph.invoke(
        {
            "messages": [HumanMessage("问题", id="question")],
            "task_notes": {"step": {"content": "只是一条模型工作笔记", "source_ids": []}},
        },
        {"configurable": {"thread_id": run["id"]}},
        context=context,
    )
    system = [m for m in captured if m.type == "system"]
    data = [m for m in captured if m.additional_kwargs.get("knowledgeops_context_data")]
    assert len(data) == 1
    assert "只是一条模型工作笔记" in data[0].content
    assert all("只是一条模型工作笔记" not in m.content for m in system)
    assert not any(m.additional_kwargs.get("knowledgeops_context_data") for m in result["messages"])
    record = context.gateway.ledger.records(root_id=run["id"])[-1]
    assert any(
        m["additional_kwargs"].get("knowledgeops_context_data")
        for m in record["request"]["messages"]
    )
