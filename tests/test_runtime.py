"""用真实 create_agent、业务库与检查点验证边界；仅供应商响应是离线替身。

若工具绕过发布授权、模型请求绕过账本、或恢复时丢失检查点，这些测试应失败。
它们证明工程控制，不证明真实模型的阅读、研究和写作质量。
"""

import importlib
import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row

from knowledgeops_agent.app.runs.service import RunService, RunStopped
from knowledgeops_agent.harness.models.gateway import (
    ModelCallFailed,
    ModelGateway,
    ModelLedger,
    prepare_accounting,
)
from knowledgeops_agent.knowledge.service import KnowledgeService
from knowledgeops_agent.sources.service import SourceService


class DraftProvider:
    """按照真实工具输出生成下一条响应，不伪造读取证据或草稿 ID。"""

    def __init__(self, segment_id: str, *, fail_after_publish: bool = False):
        self.segment_id = segment_id
        self.fail_after_publish = fail_after_publish

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        """受控测试选择：仅处理本轮真人入口的语义协议，不伪造业务授权回执。"""
        payload = json.loads(messages[-1].content)
        assert payload["ui_mode"] in {"ask", "direct", "preview"}
        return AIMessage(
            json.dumps(
                {
                    "action": "none",
                    "clear": True,
                    "message_id": payload["message"]["id"],
                    "root_id": payload["root_id"],
                    "quote": "",
                    "changeset_id": None,
                    "expected_revision": None,
                    "confirm_impact": False,
                    "ui_conflict": False,
                    "continue_task": True,
                }
            )
        )

    def stream(self, messages, **kwargs):
        observations = [message for message in messages if isinstance(message, ToolMessage)]
        if not observations:
            name, args = "read_source", {"segment_id": self.segment_id}
        elif observations[-1].name == "read_source":
            evidence = json.loads(observations[-1].content)
            name, args = (
                "prepare_changes",
                {
                    "operations": [
                        {
                            "action": "create",
                            "title": "消息投递",
                            "body": "投递需要幂等。" + evidence["citation"],
                        }
                    ]
                },
            )
        elif observations[-1].name == "prepare_changes":
            draft = json.loads(observations[-1].content)
            name, args = "publish_changes", {"changeset_id": draft["id"]}
        else:
            if self.fail_after_publish:
                raise ConnectionError("deliberate provider interruption after business commit")
            yield AIMessageChunk(content="已完成，结果请查看草稿或知识库。")
            return
        yield AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": name,
                    "args": json.dumps(args, ensure_ascii=False),
                    "id": "call-" + name,
                    "index": 0,
                }
            ],
        )


@pytest.fixture
def runtime_services(business_sessions, tmp_path):
    engine = business_sessions.kw["bind"]
    prepare_accounting(engine)
    jobs = RunService(business_sessions)
    services = (
        jobs,
        KnowledgeService(business_sessions),
        SourceService(business_sessions, tmp_path),
        ModelLedger(engine, scope="offline-" + uuid4().hex),
    )
    try:
        yield services
    finally:
        jobs.close_mcp_sessions()


def setup_run(
    services, mode="preview", *, budgets=None, fail=False, task="整理材料", auxiliary=False
):
    module = importlib.import_module("knowledgeops_agent.harness.runtime.agent")
    jobs, knowledge, sources, ledger = services
    source = sources.import_text("固定测试材料", "重复消息可能到达，因此写入需要幂等。")
    conversation = jobs.create_conversation()
    run = jobs.enqueue(
        conversation["id"],
        task,
        request_key=str(uuid4()),
        publication_mode=mode,
        auxiliary=auxiliary,
        source_ids=[source["id"]],
        budgets=budgets,
    )
    jobs.claim("worker-test", run_id=run["id"], generation=run["generation"])
    gateway = ModelGateway(
        ledger,
        enabled=True,
        client=DraftProvider(
            source["segments"][0]["id"],
            fail_after_publish=fail,
        ),
    )
    context = module.RunContext(
        run_id=run["id"],
        root_id=run["id"],
        worker_id="worker-test",
        jobs=jobs,
        knowledge=knowledge,
        sources=sources,
        gateway=gateway,
    )
    return module, context, run


def test_agent_can_discover_archived_documents_and_historical_versions(runtime_services):
    """恢复不能依赖用户知道不可见的 UUID；目录/历史工具只返回元数据。"""
    module, context, _ = setup_run(runtime_services)
    from langchain.tools import ToolRuntime

    tool_runtime = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="history-check",
        store=None,
    )
    draft = context.knowledge.create_draft(
        "fixture", [{"action": "create", "title": "历史材料", "body": "旧上限80次。"}]
    )
    context.knowledge.approve(draft["id"])
    context.knowledge.publish(draft["id"], idempotency_key="fixture-create")
    doc = context.knowledge.list_documents()[0]
    archived = context.knowledge.create_draft(
        "fixture",
        [{"action": "archive", "document_id": doc["id"], "base_version_id": doc["version_id"]}],
    )
    context.knowledge.approve(archived["id"])
    context.knowledge.publish(archived["id"], idempotency_key="fixture-archive")
    rows = module.list_knowledge.func(tool_runtime, include_archived=True)
    assert len(rows) == 1 and rows[0]["archived"]
    assert "body" not in rows[0]
    history = module.knowledge_history.func(doc["id"], tool_runtime)
    assert [item["version"] for item in history] == [2, 1]
    assert history[1]["version_id"] == doc["version_id"]
    assert all("body" not in item for item in history)


@pytest.mark.parametrize("mode", ["preview", "direct"])
def test_agent_tools_enforce_publication_mode_and_preserve_actual_inputs(runtime_services, mode):
    module, context, run = setup_run(runtime_services, mode)
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    state = graph.invoke(
        {"messages": [HumanMessage(run["task"])]},
        config={"configurable": {"thread_id": run["checkpoint_id"]}},
        context=context,
    )
    drafts = context.knowledge.list_drafts(root_id=run["root_id"])
    assert len(drafts) == 1
    assert drafts[0]["status"] == ("published" if mode == "direct" else "draft")
    assert len(context.knowledge.list_documents()) == int(mode == "direct")
    assert isinstance(state["messages"][-2], ToolMessage)
    if mode == "preview":
        assert state["messages"][-2].status == "error"
    records = context.gateway.ledger.records(root_id=run["root_id"])
    assert len(records) == 4
    assert records[-1]["request"]["messages"][-1]["type"] == "tool"
    assert "read_source" in json.dumps(records[0]["request"]["tools"])
    assert context.jobs.get_run(run["id"])["tool_calls"] == 3
    # 最后一轮必须收到当前业务状态；历史回复中“尚未发布”不能覆盖真实回执。
    system = records[-1]["request"]["messages"][0]["content"]
    controls = json.loads(system.split("程序提供的本轮控制信息：\n", 1)[1])
    assert controls["changesets"][0]["id"] == drafts[0]["id"]
    assert controls["changesets"][0]["status"] == drafts[0]["status"]
    followup = context.jobs.enqueue(run["conversation_id"], "继续讨论", request_key=str(uuid4()))
    assert context.jobs.related_changesets(followup["id"]) == controls["changesets"]


def test_tool_budget_stop_is_not_turned_into_model_observation(runtime_services):
    module, context, run = setup_run(runtime_services, budgets={"tool_calls": 1})
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    with pytest.raises(RunStopped, match="tool budget"):
        graph.invoke(
            {"messages": [HumanMessage(run["task"])]},
            config={"configurable": {"thread_id": run["checkpoint_id"]}},
            context=context,
        )
    assert context.knowledge.list_drafts(root_id=run["root_id"]) == []
    assert len(context.gateway.ledger.records()) == 2


def test_checkpoint_reopen_resumes_after_committed_publication_without_duplicate(runtime_services):
    module, context, run = setup_run(runtime_services, "direct", fail=True)
    engine = context.gateway.ledger.engine
    args, kwargs = engine.dialect.create_connect_args(engine.url)
    # 测试 schema 与业务 fixture 一致；从连接读取当前 schema 不接触生产库。
    with engine.connect() as connection:
        schema = connection.exec_driver_sql("SELECT current_schema()").scalar_one()
    config = {"configurable": {"thread_id": run["checkpoint_id"]}}

    def connect():
        return Connection.connect(
            *args,
            **kwargs,
            options=f"-csearch_path={schema}",
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
        )

    with connect() as connection:
        saver = PostgresSaver(connection)
        saver.setup()
        graph = module.build_agent(context, checkpointer=saver)
        with pytest.raises(ModelCallFailed):
            graph.invoke({"messages": [HumanMessage(run["task"])]}, config, context=context)
    assert context.knowledge.list_drafts(root_id=run["id"])[0]["status"] == "published"
    # 模拟服务退出；恢复明确由用户入口完成，不依赖模型或旧消息自动重试。
    context.jobs.recover_on_startup()
    resumed = context.jobs.resume(run["id"])
    context.jobs.claim("worker-test", run_id=run["id"], generation=resumed["generation"])
    context.gateway.client.fail_after_publish = False
    with connect() as connection:
        graph = module.build_agent(context, checkpointer=PostgresSaver(connection))
        state = graph.invoke(None, config, context=context)
    assert state["messages"][-1].content.startswith("已完成")
    assert len(context.knowledge.list_documents()) == 1
    assert len(context.knowledge.list_drafts(root_id=run["id"])) == 1
    assert context.jobs.get_run(run["id"])["tool_calls"] == 3


def test_replaced_worker_cannot_continue_execution(runtime_services):
    module, context, run = setup_run(runtime_services)
    context.jobs.recover_on_startup()
    resumed = context.jobs.resume(run["id"])
    context.jobs.claim("new-worker", run_id=run["id"], generation=resumed["generation"])
    with pytest.raises(RunStopped, match="owner"):
        context.jobs.guard(run["id"], worker_id="worker-test")


def test_research_children_isolate_history_share_ledger_and_replay_without_calls(runtime_services):
    from langchain_core.messages import AIMessage

    module, context, run = setup_run(runtime_services)
    delegation = importlib.import_module("knowledgeops_agent.harness.subagents.delegation")
    context.checkpointer = InMemorySaver()

    class ResearchProvider:
        def bind_tools(self, tools):
            names = {tool.name for tool in tools}
            assert {"read_source", "read_knowledge"} <= names
            assert not {"prepare_changes", "publish_changes", "delegate_research"} & names
            return self

        def stream(self, messages, **kwargs):
            assert all("主对话隐私标识" not in str(m.content) for m in messages)
            yield AIMessageChunk(content="研究结果：需要引用证据，资料不足。")

    context.gateway.client = ResearchProvider()
    # 主图的历史既不自动复制给研究者，也不替研究者创建另一套账本。
    parent_state = {"messages": [HumanMessage("主对话隐私标识"), AIMessage("未发给研究者")]}
    assert parent_state["messages"]
    from fixtures.subagent_dispatch import drive_delegation

    outputs = drive_delegation(context,
        lambda: delegation.research_many(context, ["研究第一主题", "研究第二主题"], "call-research"), identity="research")
    assert len(outputs) == 2 and all(row["status"] == "completed" for row in outputs)
    children = [r for r in context.jobs.list_runs(root_id=run["id"]) if r["parent_id"]]
    assert len(children) == 2
    records = context.gateway.ledger.records(root_id=run["id"])
    assert len(records) == 2 and {r["run_id"] for r in records} == {c["id"] for c in children}
    assert all(r["purpose"] == "research" for r in records)
    again = drive_delegation(context,
        lambda: delegation.research_many(context, ["研究第一主题", "研究第二主题"], "call-research"), identity="research")
    assert again == outputs
    assert len(context.gateway.ledger.records()) == 2


def test_publication_checks_cancellation_inside_business_transaction(runtime_services):
    module, context, run = setup_run(runtime_services, "direct")
    draft = context.knowledge.create_draft(
        run["id"], [{"action": "create", "title": "应停止", "body": "未发布"}]
    )
    context.jobs.cancel(run["id"])
    with pytest.raises(RunStopped, match="cancel"):
        context.knowledge.publish(
            draft["id"],
            idempotency_key="cancelled-publication",
            guard=lambda session: context.jobs.publication_guard(
                session, run["id"], context.worker_id
            ),
        )
    assert context.knowledge.list_documents() == []


def test_conversation_can_be_cited_without_future_messages_or_child_history_leak(runtime_services):
    from types import SimpleNamespace

    module, context, run = setup_run(runtime_services)
    context.jobs.enqueue(
        run["conversation_id"], "未来消息不应进入当前证据", request_key=str(uuid4())
    )
    capture = module.capture_conversation.func(SimpleNamespace(context=context))
    source = context.sources.get_source(capture["source_id"])
    assert source["kind"] == "conversation"
    evidence = context.sources.read_segment(source["segments"][0]["id"], root_id=run["id"])
    assert "整理材料" in evidence["text"] and "未来消息" not in evidence["text"]
    again = module.capture_conversation.func(SimpleNamespace(context=context))
    assert again["version_id"] == capture["version_id"]
    context.run_id = context.jobs.start_child(run["id"], "只读研究")["id"]
    with pytest.raises(PermissionError):
        module.capture_conversation.func(SimpleNamespace(context=context))


def test_final_response_repair_keeps_user_history_and_counts_each_request(runtime_services):
    """真实图阻止伪造引用，并把校验反馈放在控制上下文而非伪造用户授权。"""
    module, context, run = setup_run(runtime_services)

    class RepairProvider:
        calls = 0

        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            self.calls += 1
            assert [
                m.content
                for m in messages
                if isinstance(m, HumanMessage)
                and not m.additional_kwargs.get("knowledgeops_context_data")
            ] == [run["task"]]
            if self.calls == 1:
                yield AIMessageChunk(content="规则[伪造](/api/evidence/not-a-real-id)")
            else:
                assert "final_response_feedback" in messages[0].content
                assert "not-a-real-id" in messages[0].content
                yield AIMessageChunk(content="未取得来源证据，不能给出结论。")

    context.gateway.client = RepairProvider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    state = graph.invoke(
        {"messages": [HumanMessage(run["task"])]},
        config={"configurable": {"thread_id": run["checkpoint_id"]}},
        context=context,
    )
    assert state["messages"][-1].content == "未取得来源证据，不能给出结论。"
    assert len(context.gateway.ledger.records(root_id=run["id"])) == 2
    assert not state.get("final_response_feedback")
    assert any(e["kind"] == "answer_validation" for e in context.jobs.events(run["id"]))


def test_visible_answer_length_honors_explicit_limit_without_charging_urls():
    module = importlib.import_module("knowledgeops_agent.harness.models.validation")
    assert module.explicit_length_limit("请回答，最终答案不超过500字。") == 500
    assert module.explicit_length_limit("改为150字以内解释，不要小标题。") == 150
    assert module.explicit_length_limit("请在80字之内回答，最多100字。") == 80
    assert module.explicit_length_limit("用200字以下说明。") == 200
    assert module.explicit_length_limit("保持简洁") is None
    assert module.visible_length("**你好**[来源](/api/evidence/a-very-long-uuid)") == 4


def test_unrepairable_final_answer_stops_after_two_counted_corrections(runtime_services):
    module, context, run = setup_run(runtime_services)

    class InvalidProvider:
        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            yield AIMessageChunk(content="[伪造](/api/evidence/unknown)")

    context.gateway.client = InvalidProvider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    with pytest.raises(ValueError, match="final answer validation failed"):
        graph.invoke(
            {"messages": [HumanMessage(run["task"])]},
            config={"configurable": {"thread_id": run["checkpoint_id"]}},
            context=context,
        )
    assert len(context.gateway.ledger.records(root_id=run["id"])) == 3


def test_new_task_history_recovers_provider_metadata_from_the_recorded_response(runtime_services):
    from dataclasses import replace

    module, context, run = setup_run(runtime_services)

    class HistoryProvider:
        def stream(self, messages, **kwargs):
            yield AIMessageChunk(
                content="上轮答案", additional_kwargs={"reasoning_content": "opaque-test"}
            )

    context.gateway.client = HistoryProvider()
    context.gateway.invoke(
        [HumanMessage(run["task"])], root_id=run["id"], run_id=run["id"], stream=True
    )
    context.jobs.finish(
        run["id"], context.worker_id, status="completed", outcome={"answer": "上轮答案"}
    )
    next_run = context.jobs.enqueue(run["conversation_id"], "继续提问", request_key=str(uuid4()))
    messages = module.initial_messages(
        replace(context, run_id=next_run["id"], root_id=next_run["id"])
    )
    assert messages[-2].additional_kwargs["reasoning_content"] == "opaque-test"
    assert messages[-1].content == "继续提问"


def test_normalized_final_citation_keeps_provider_metadata_in_next_user_turn(runtime_services):
    """引用渲染后的可见正文仍须匹配原始响应，不能丢失DeepSeek协议字段。"""
    from dataclasses import replace

    module, context, run = setup_run(runtime_services)
    source = context.sources.list_sources()[0]
    segment = context.sources.get_source(source["id"])["segments"][0]
    evidence = context.sources.read_segment(segment["id"], root_id=run["id"])

    class MarkerProvider:
        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            yield AIMessageChunk(
                content=f"结论[citation:{evidence['id']}]",
                additional_kwargs={"reasoning_content": "opaque-original"},
            )

    context.gateway.client = MarkerProvider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    state = graph.invoke(
        {"messages": [HumanMessage(run["task"])]},
        {"configurable": {"thread_id": run["checkpoint_id"]}},
        context=context,
    )
    answer = state["messages"][-1].text
    assert "[citation:" not in answer and evidence["id"] in answer
    context.jobs.finish(run["id"], context.worker_id, outcome={"answer": answer})
    next_run = context.jobs.enqueue(run["conversation_id"], "继续", request_key=str(uuid4()))
    messages = module.initial_messages(
        replace(context, run_id=next_run["id"], root_id=next_run["id"])
    )
    assert messages[-2].content == answer
    assert messages[-2].additional_kwargs["reasoning_content"] == "opaque-original"


def test_queued_followup_includes_prior_answer_without_future_turns(runtime_services):
    """先排队、后收到上轮回复时，历史按任务轮次组织，而非消息落库时间截断。"""
    from dataclasses import replace
    from types import SimpleNamespace

    module, context, run = setup_run(runtime_services)
    followup = context.jobs.enqueue(
        run["conversation_id"], "根据刚才的回答继续整理", request_key=str(uuid4())
    )
    context.jobs.enqueue(run["conversation_id"], "未来任务禁止泄漏", request_key=str(uuid4()))
    context.jobs.finish(
        run["id"], context.worker_id, status="completed", outcome={"answer": "上轮随后完成的答案"}
    )
    next_context = replace(context, run_id=followup["id"], root_id=followup["id"])
    messages = module.initial_messages(next_context)
    assert [message.content for message in messages] == [
        run["task"],
        "上轮随后完成的答案",
        followup["task"],
    ]
    capture = module.capture_conversation.func(SimpleNamespace(context=next_context))
    source = context.sources.get_source(capture["source_id"])
    text = context.sources.read_segment(source["segments"][0]["id"], root_id=followup["id"])["text"]
    assert "上轮随后完成的答案" in text and "未来任务禁止泄漏" not in text
    assert text.index("上轮随后完成的答案") < text.index(followup["task"])


def test_finalization_uses_real_evidence_catalog_and_reserves_last_requests(runtime_services):
    from types import SimpleNamespace

    module, context, run = setup_run(runtime_services, budgets={"model_requests": 2})
    source = context.sources.list_sources()[0]
    segment = context.sources.get_source(source["id"])["segments"][0]
    evidence = context.sources.read_segment(segment["id"], root_id=run["id"])

    class Request:
        runtime = SimpleNamespace(context=context)
        state = {"final_response_feedback": []}
        messages = []
        system_message = None
        tools = [module.read_source]

        def override(self, **kwargs):
            return kwargs

    controls = module.RuntimeControlMiddleware().wrap_model_call(Request(), lambda value: value)
    assert controls["tools"] == []
    data = json.loads(controls["system_message"].content.split("程序提供的本轮控制信息：\n", 1)[1])
    background = json.loads(controls["messages"][0].content.split("\n", 1)[1])
    assert background["evidence_catalog"][0]["citation"] == evidence["citation"]
    assert data["finalize_now"] is True
    assert "text" not in background["evidence_catalog"][0]


def test_existing_reference_inside_inline_code_does_not_absorb_following_prose(runtime_services):
    """归档回复可以说明原有链接，反引号和中文标点不应成为证据 ID 的一部分。"""
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    module, context, run = setup_run(runtime_services)
    source = context.sources.list_sources()[0]
    segment = context.sources.get_source(source["id"])["segments"][0]
    evidence = context.sources.read_segment(segment["id"], root_id=run["id"])
    state = {
        "messages": [AIMessage(content=f"原链接 `/api/evidence/{evidence['id']}`（来源仍保留）。")]
    }
    update = module.FinalAnswerValidation().after_model(state, SimpleNamespace(context=context))
    assert update["final_response_feedback"] == []
    assert update["messages"][0].text.startswith(state["messages"][0].text)


def test_answer_repair_disables_further_tools_and_gives_explicit_revision_instruction(
    runtime_services,
):
    """收束修订只变更答案；即使预算充足也不重新派发业务操作。"""
    from types import SimpleNamespace

    module, context, run = setup_run(runtime_services)

    class Request:
        runtime = SimpleNamespace(context=context)
        state = {"final_response_feedback": ["已超出用户字数上限，需压缩。"]}
        messages = []
        system_message = None
        tools = [module.read_source]

        def override(self, **kwargs):
            return kwargs

    result = module.RuntimeControlMiddleware().wrap_model_call(Request(), lambda value: value)
    assert result["tools"] == []
    assert "本轮只输出修订后的最终答案" in result["system_message"].content
