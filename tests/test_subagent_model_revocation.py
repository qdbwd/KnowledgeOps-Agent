"""撤销在每次实际请求前生效，包括一次外层模型调用中的多个摘要请求。"""

import json
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run
from test_subagent_runtime import role

from knowledgeops_agent.context_management import summarize
from knowledgeops_agent.run_resources import ResourceUnavailable
from knowledgeops_agent.subagent_registry import SubagentRegistry

runtime_services = _runtime_services


class RevokeDuringSummary:
    """只替换供应商响应；停用通过真实角色目录事务，模型请求仍走真实账本。"""

    def __init__(self, registry, definition):
        self.registry, self.definition = registry, definition
        self.summary_calls = 0
        self.research_calls = 0

    def invoke(self, messages, **kwargs):
        self.summary_calls += 1
        if self.summary_calls == 1:
            self.registry.update(
                self.definition["id"],
                expected_revision=self.definition["revision"],
                enabled=False,
            )
        return AIMessage("保留原任务的只读要求与材料范围，历史原文仍可追溯。")

    def bind_tools(self, tools):
        return self

    def stream(self, messages, **kwargs):
        self.research_calls += 1
        yield AIMessageChunk(content="这次研究请求在角色撤销后不应发送。")


def configured_child(runtime_services, *, summary_fraction=0.85):
    module, parent, root = setup_run(
        runtime_services, task="仅核对分配历史，不保存。", budgets={"model_requests": 10}
    )
    definition = role(parent, "summary-reader", tools=[], skills=[])
    provider = RevokeDuringSummary(SubagentRegistry(parent.jobs.sessions), definition)
    parent.gateway.client = provider
    parent.gateway.capacity = parent.gateway.capacity.model_copy(
        # 本用例验证请求间撤回，固定旧总量触发合同，避免随默认窗口调整而失去分支覆盖。
        update={
            "summary_fraction": summary_fraction,
            "working_window": 32768,
            "summary_trigger_basis": "total_tokens",
        }
    )
    saver = InMemorySaver()
    module.build_agent(parent, checkpointer=saver)
    child = parent.jobs.start_child(
        root["id"],
        "仅核对分配历史，不保存。",
        operation_key="summary-revocation-child",
        subagent=definition,
    )
    context = replace(parent, run_id=child["id"])
    graph = module.build_agent(context, checkpointer=saver)
    return context, child, graph, provider


def assert_only_first_summary_was_charged(context, provider):
    records = context.gateway.ledger.records(root_id=context.root_id)
    assert provider.summary_calls == 1
    assert provider.research_calls == 0
    assert len(records) == 1
    assert records[0]["run_id"] == context.run_id
    assert records[0]["purpose"] == "context_summary"
    assert records[0]["status"] == "succeeded"
    assert context.gateway.ledger.summary(context.root_id)["root_used"] == 1


def test_role_disabled_during_first_summary_blocks_next_summary_batch(runtime_services):
    context, _, _, provider = configured_child(runtime_services)
    # 超过实际工作窗口，确保首批不是最后一批；不伪造容量计算或摘要分块结果。
    prefix = [HumanMessage("早期材料：" + "abcdef0123456789" * 30000)]
    original = json.dumps([m.model_dump(mode="json") for m in prefix], ensure_ascii=False)
    artifact_id = context.jobs.save_artifact(context.run_id, "context_original", original)
    assert (
        context.gateway.measure(prefix)["input_tokens"]
        > context.gateway.capacity.working_window * 2
    )
    with pytest.raises(ResourceUnavailable, match="撤回"):
        summarize(context, prefix, artifact_id)
    assert_only_first_summary_was_charged(context, provider)
    assert context.jobs.artifact_content(artifact_id, root_id=context.root_id) == original


def test_role_disabled_by_last_summary_blocks_following_research_model(runtime_services):
    context, child, graph, provider = configured_child(runtime_services, summary_fraction=0.5)
    # 提前触发压缩但让完整历史能由一批摘要处理，覆盖最后摘要→研究模型的间隙。
    original = "旧讨论材料：" + "abcdef0123456789" * 500
    messages = [HumanMessage(original), *[HumanMessage(f"后续讨论{i}") for i in range(10)]]
    config = {"configurable": {"thread_id": child["checkpoint_id"]}}
    with pytest.raises(ResourceUnavailable, match="撤回"):
        graph.invoke({"messages": messages}, config, context=context)
    assert_only_first_summary_was_charged(context, provider)
    assert graph.get_state(config).values["messages"][0].content == original
    artifacts = context.jobs.artifacts(context.run_id)
    assert any(row["kind"] == "context_original" for row in artifacts)
    # 已保存完整摘要，证明本例停在摘要结束后的研究请求，而不是下一摘要批。
    assert any(row["kind"] == "working_summary" for row in artifacts)
