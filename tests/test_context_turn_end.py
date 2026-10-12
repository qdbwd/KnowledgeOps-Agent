"""一轮回复后的主动整理不能依赖下一条用户消息，也不能覆盖原回复或原历史。"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.context_management import ContextMiddleware, working_messages

runtime_services = _runtime_services


def test_real_graph_runs_compaction_after_final_answer_crosses_the_threshold(runtime_services):
    """只替换供应商，真实图/计数/轮末钩子证明不需要第二条用户消息来触发。"""
    module, context, run = setup_run(runtime_services)
    context.gateway.capacity = context.gateway.capacity.model_copy(
        update={
            "working_window": 65536,
            "output_reserve": 28000,
            "framing_reserve": 512,
        }
    )

    class Provider:
        replied = False
        tools = []

        def bind_tools(self, tools):
            self.tools = tools
            return self

        def stream(self, messages, **kwargs):
            capacity = context.gateway.capacity
            before = context.gateway.measure(messages, tools=self.tools)
            threshold = int(capacity.working_window * capacity.summary_fraction)
            assert before["input_tokens"] < threshold
            assert before["total_tokens"] <= capacity.working_window
            answer = "a " * (threshold - before["input_tokens"] + 100)
            after = context.gateway.measure([*messages, AIMessage(answer)], tools=self.tools)
            assert after["input_tokens"] > threshold
            assert after["input_tokens"] - before["input_tokens"] <= capacity.output_reserve
            self.replied = True
            yield AIMessageChunk(answer)

        def invoke(self, messages, **kwargs):
            assert self.replied, "这个用例只能在主模型答复以后调用摘要"
            return AIMessage("早期约束已保留，本轮回复已经给出。")

    context.gateway.client = Provider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    state = graph.invoke(
        {"messages": [HumanMessage("早期记录：" + "a " * 28000), HumanMessage("继续回答。")]},
        {"configurable": {"thread_id": run["id"]}},
        context=context,
    )
    assert state["working_summary"]
    records = context.gateway.ledger.records(root_id=run["id"])
    assert records[0]["purpose"] == "agent"
    assert all(row["purpose"] == "context_summary" for row in records[1:])
    assert len(state["messages"]) == 3


def turn_end_fixture(runtime_services, *, budgets=None):
    """保留真实账本和状态，仅替换供应商；按用户要求用2万窗口省去巨量填充。

    容量副本仅属于隔离测试，不修改正式256K配置。输出和协议预留也按
    小窗口缩小；此处验证压缩流程与失败保护，不验证真实模型摘要质量。
    """
    _, context, run = setup_run(runtime_services, budgets=budgets)
    context.gateway.capacity = context.gateway.capacity.model_copy(
        update={"working_window": 20000, "output_reserve": 1024, "framing_reserve": 512}
    )
    # 已用正式词表核对：该填充约19000 tokens，超过2万窗口的90%阈值；
    # 不把中文字符数当token数，否则可能根本没有触发压缩。
    previous = HumanMessage("原来的用户要求：不要自行发布。" + "a " * 19000, id="past")
    latest = HumanMessage("这一轮解释完成即可。", id="latest")
    request = context.gateway.request_packet(
        [SystemMessage("规则"), previous, latest], max_tokens=1024
    )
    reservation = context.gateway.ledger.reserve(run["id"], run["id"], "agent", request)
    context.gateway.ledger.finish(reservation, response={"content": "这轮答案"}, usage=None)
    state = {"messages": [previous, latest, AIMessage("这轮答案", id="answer")]}
    return context, run, state


def test_turn_end_compacts_without_another_main_model_call(runtime_services):
    context, run, state = turn_end_fixture(runtime_services)

    class SummaryOnly:
        def invoke(self, messages, **kwargs):
            return AIMessage("用户要求不自行发布；本轮解释已完成。")

    context.gateway.client = SummaryOnly()
    update = ContextMiddleware().after_agent(state, SimpleNamespace(context=context))
    assert update and update["working_summary"]
    assert "messages" not in update  # 原回复与原话仍在State中，不用摘要替换。
    view = working_messages(state["messages"], update["context_compaction"])
    assert view[-1].id == "answer"
    assert state["messages"][0].content not in str(view)
    records = context.gateway.ledger.records(root_id=run["id"])
    assert records[0]["purpose"] == "agent"
    assert all(row["purpose"] == "context_summary" for row in records[1:])
    event = next(e for e in context.jobs.events(run["id"]) if e["kind"] == "context_compacted")
    assert event["data"]["trigger"] == "after_turn"
    assert event["data"]["before"]["working_window"] == 20000
    assert event["data"]["before"]["summary_trigger_tokens"] == 18000
    assert event["data"]["before"]["input_tokens"] > 18000
    assert event["data"]["after"]["input_tokens"] < 18000
    assert event["data"]["based_on_request_id"] == records[0]["id"]


@pytest.mark.parametrize("case", ["short", "summary_failure", "budget_reserved"])
def test_turn_end_preserves_answer_when_compaction_is_unneeded_or_unavailable(
    runtime_services, case
):
    context, run, state = turn_end_fixture(
        runtime_services, budgets={"model_requests": 2} if case == "budget_reserved" else None
    )

    class Unavailable:
        def invoke(self, messages, **kwargs):
            if case != "summary_failure":
                pytest.fail("short history or reserved budget must not call the summarizer")
            raise ConnectionError("offline fixture")

    context.gateway.client = Unavailable()
    if case == "short":
        state["messages"] = state["messages"][1:]
    before = list(state["messages"])
    assert ContextMiddleware().after_agent(state, SimpleNamespace(context=context)) is None
    assert state["messages"] == before
    assert state["messages"][-1].content == "这轮答案"
