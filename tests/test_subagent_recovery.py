"""角色绑定的恢复回归：真实子图、业务服务和账本，仅供应商为离线替身。

删除同键角色比对、重建子运行/检查点、恢复时套用新提示或重置已用额度，
均应破坏这里的外部行为断言。本文件不发起真实模型请求。
"""

import json
from dataclasses import replace

import pytest
from fixtures.subagent_dispatch import drive_delegation
from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run
from test_subagent_runtime import role

from knowledgeops_agent.harness.subagents.delegation import delegate_one
from knowledgeops_agent.harness.subagents.registry import SubagentRegistry
from knowledgeops_agent.persistence.models import Run

runtime_services = _runtime_services


def test_same_tool_call_cannot_rebind_completed_child_to_another_role(runtime_services):
    """同一真实调用键换角色必须拒绝，不能返回旧角色结果或偷偷新建/计费。"""
    module, context, root = setup_run(runtime_services)
    original = role(context, "original", tools=[])
    replacement = role(context, "replacement", tools=[])

    class Provider:
        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            yield AIMessageChunk(content="已完成委派核对，未执行外部操作。")

    context.gateway.client = Provider()
    module.build_agent(context, checkpointer=InMemorySaver())
    first = drive_delegation(context,
        lambda: delegate_one(context, original, "检查同一项材料", "stable-role-call"), identity="original")
    assert first["status"] == "completed"
    before_child = context.jobs.get_run(first["run_id"])
    before_requests = context.gateway.ledger.records(root_id=root["id"])
    assert len(before_requests) == 1

    with pytest.raises(ValueError, match="different task"):
        delegate_one(context, replacement, "检查同一项材料", "stable-role-call")

    children = [run for run in context.jobs.list_runs(root_id=root["id"]) if run["parent_id"]]
    assert len(children) == 1
    assert children[0]["id"] == before_child["id"]
    assert children[0]["checkpoint_id"] == before_child["checkpoint_id"]
    assert children[0]["input"]["subagent"] == original
    assert children[0]["status"] == "completed"
    assert context.gateway.ledger.records(root_id=root["id"]) == before_requests
    assert context.gateway.ledger.summary(root["id"])["root_used"] == 1


def test_interrupted_child_resumes_frozen_role_and_checkpoint_without_resetting_budget(
    runtime_services,
):
    """先读取后中断，再手动恢复；不能重读、换新版提示或遗失失败请求费用。"""
    module, context, root = setup_run(
        runtime_services, budgets={"model_requests": 4, "tool_calls": 8}
    )
    original = role(context, "resumer", tools=["read_source"])
    registry = SubagentRegistry(context.jobs.sessions)
    source = context.sources.get_source(root["input"]["source_ids"][0])
    segment_id = source["segments"][0]["id"]
    task = f"读取片段 {segment_id}，简洁报告结论并保留来源引用。"

    class InterruptAfterReadProvider:
        fail_after_read = True

        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            observations = [message for message in messages if isinstance(message, ToolMessage)]
            if not observations:
                yield AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "read_source",
                            "args": json.dumps({"segment_id": segment_id}),
                            "id": "read-before-interruption",
                            "index": 0,
                        }
                    ],
                )
                return
            if self.fail_after_read:
                raise ConnectionError("deliberate offline interruption after source read")
            evidence = json.loads(observations[-1].content)
            yield AIMessageChunk(content="重复消息可能到达，写入需要幂等。" + evidence["citation"])

    provider = InterruptAfterReadProvider()
    context.gateway.client = provider
    saver = InMemorySaver()
    module.build_agent(context, checkpointer=saver)
    failed = drive_delegation(context,
        lambda: delegate_one(context, original, task, "resume-role-call"), identity="resume-role")
    assert failed["status"] == "failed"
    before_child = context.jobs.get_run(failed["run_id"])
    before_root = context.jobs.get_run(root["id"])
    before_requests = context.gateway.ledger.records(root_id=root["id"])
    assert len(before_requests) == 2
    assert [record["status"] for record in before_requests] == ["succeeded", "failed"]
    assert before_root["tool_calls"] == 1
    assert context.gateway.ledger.summary(root["id"])["root_used"] == 2

    # 新提示只对新根任务生效；已绑定的旧修订仍是允许恢复的版本。
    updated = registry.update(
        original["id"],
        expected_revision=original["revision"],
        system_prompt="UPDATED-ROLE-DO-NOT-USE：只供以后新建的任务使用。",
    )
    assert updated["revision"] == original["revision"] + 1
    assert updated["authorization_epoch"] == original["authorization_epoch"]
    context.jobs.recover_on_startup()
    assert context.jobs.get_run(root["id"])["status"] == "interrupted"
    assert context.gateway.ledger.records(root_id=root["id"]) == before_requests
    resumed = context.jobs.resume(root["id"])
    claimed = context.jobs.claim(
        "worker-after-restart", run_id=root["id"], generation=resumed["generation"]
    )
    assert claimed is not None
    assert resumed["generation"] == before_root["generation"] + 1
    assert claimed["tool_calls"] == before_root["tool_calls"]
    assert claimed["budgets"] == before_root["budgets"]
    assert context.gateway.ledger.summary(root["id"])["root_used"] == 2

    provider.fail_after_read = False
    restarted = replace(context, worker_id="worker-after-restart", resources=None)
    module.build_agent(restarted, checkpointer=saver)
    frozen = next(item for item in restarted.resources["subagents"] if item["id"] == original["id"])
    assert frozen == original
    completed = drive_delegation(restarted,
        lambda: delegate_one(restarted, frozen, task, "resume-role-call"), identity="resume-role")
    assert completed["status"] == "completed"
    assert "写入需要幂等" in completed["answer"]
    after_child = restarted.jobs.get_run(completed["run_id"])
    assert after_child["id"] == before_child["id"]
    assert after_child["checkpoint_id"] == before_child["checkpoint_id"]
    assert after_child["generation"] == resumed["generation"]
    with restarted.jobs.sessions() as session:
        persisted_child = session.get(Run, after_child["id"])
        assert persisted_child is not None
        assert persisted_child.worker_id and persisted_child.worker_id != "worker-after-restart"
    assert after_child["input"]["subagent"] == original
    assert after_child["budgets"] == before_child["budgets"]
    assert (
        len([run for run in restarted.jobs.list_runs(root_id=root["id"]) if run["parent_id"]]) == 1
    )

    # checkpoint 中已有读取结果：恢复只补一个模型请求，不重新执行 read_source。
    records = restarted.gateway.ledger.records(root_id=root["id"])
    assert len(records) == 3
    assert records[:2] == before_requests
    assert records[-1]["status"] == "succeeded"
    assert {record["run_id"] for record in records} == {before_child["id"]}
    request = json.dumps(records[-1]["request"], ensure_ascii=False)
    assert "ROLE:resumer" in request
    assert "UPDATED-ROLE-DO-NOT-USE" not in request
    assert restarted.jobs.get_run(root["id"])["tool_calls"] == 1
    assert restarted.gateway.ledger.summary(root["id"])["root_used"] == 3
    assert len(restarted.gateway.ledger.records()) == 3
    events = restarted.jobs.events(before_child["id"])
    reads = [
        event
        for event in events
        if event["kind"] == "tool_started" and event["data"]["name"] == "read_source"
    ]
    assert len(reads) == 1
