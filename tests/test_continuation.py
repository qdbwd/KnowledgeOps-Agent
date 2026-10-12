"""排队接续与旧任务恢复不能靠可见聊天倒推；验证实际持久序号/快照边界。"""

import importlib
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from test_jobs import jobs as _jobs
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

jobs = _jobs
runtime_services = _runtime_services


def test_broker_delivery_order_cannot_skip_earlier_same_conversation_task(jobs):
    _, _, service, _, _ = jobs
    conversation = service.create_conversation()["id"]
    first = service.enqueue(conversation, "先整理", request_key="one")
    second = service.enqueue(conversation, "再核对", request_key="two")
    assert service.claim("w", run_id=second["id"], generation=1) is None
    assert service.claim("w", run_id=first["id"], generation=1)["id"] == first["id"]


def test_snapshot_binds_at_claim_and_only_frozen_artifacts_cross_runs(jobs):
    _, _, service, _, _ = jobs
    conversation = service.create_conversation()["id"]
    first = service.enqueue(conversation, "先整理", request_key="one", publication_mode="direct")
    service.claim("w", run_id=first["id"], generation=1)
    second = service.enqueue(
        conversation, "接着核对", request_key="two", publication_mode="preview"
    )
    artifact = service.save_artifact(first["id"], "tool_result", "先前的完整原文")
    service.finish(first["id"], "w", outcome={"answer": "刚完成的第一轮结果"})
    # 如果在enqueue时冻结，这条排队消息就看不到前一轮后来产生的结果。
    service.claim("w", run_id=second["id"], generation=1)
    snapshot = service.continuation(second["id"])
    assert snapshot["content"]["outcome"]["answer"] == "刚完成的第一轮结果"
    assert artifact in snapshot["content"]["artifact_ids"]
    assert service.read_artifact(artifact, root_id=second["id"])["text"] == "先前的完整原文"
    later = service.save_artifact(first["id"], "tool_result", "绑定之后新出现的资料")
    with pytest.raises(PermissionError):
        service.read_artifact(later, root_id=second["id"])
    assert service.get_run(second["id"])["input"]["publication_mode"] == "preview"


def test_old_run_resume_keeps_parent_and_does_not_replace_newer_snapshot(jobs):
    _, _, service, _, _ = jobs
    conversation = service.create_conversation()["id"]
    old = service.enqueue(conversation, "较旧任务", request_key="old")
    service.claim("w", run_id=old["id"], generation=1)
    service.finish(old["id"], "w", status="interrupted", outcome={"answer": "旧部分结果"})
    newer = service.enqueue(conversation, "新任务", request_key="newer")
    service.claim("w", run_id=newer["id"], generation=1)
    service.finish(newer["id"], "w", outcome={"answer": "新进展"})
    service.resume(old["id"])
    service.claim("w", run_id=old["id"], generation=2)
    assert service.continuation(old["id"]) is None
    service.finish(old["id"], "w", outcome={"answer": "旧任务后来完成"})
    latest = service.enqueue(conversation, "继续最新进展", request_key="latest")
    service.claim("w", run_id=latest["id"], generation=1)
    assert service.continuation(latest["id"])["run_id"] == newer["id"]


def test_real_state_snapshot_retains_complete_groups_not_pending_actions(runtime_services):
    module, context, run = setup_run(runtime_services)
    continuation = importlib.import_module("knowledgeops_agent.continuation")
    state = {
        "messages": [
            HumanMessage("原问题", id="original-user"),
            AIMessage(
                "", tool_calls=[{"name": "read_source", "args": {}, "id": "read-one"}], id="read"
            ),
            ToolMessage("重要条件与例子", tool_call_id="read-one", id="observed"),
            AIMessage(
                "未执行写入",
                tool_calls=[{"name": "publish_changes", "args": {}, "id": "not-done"}],
                id="pending",
            ),
        ]
    }
    snapshot = continuation.from_state(context, state)
    context.jobs.finish(run["id"], context.worker_id, status="interrupted", continuation=snapshot)
    next_run = context.jobs.enqueue(
        run["conversation_id"], "只解释这个条件", request_key="next", publication_mode="preview"
    )
    context.jobs.claim(context.worker_id, run_id=next_run["id"], generation=1)
    messages = module.initial_messages(
        replace(context, run_id=next_run["id"], root_id=next_run["id"])
    )
    assert any(
        isinstance(message, ToolMessage) and message.content == "重要条件与例子"
        for message in messages
    )
    assert "not-done" not in str(messages)
    assert messages[-1].content == "只解释这个条件"


@pytest.mark.parametrize("empty_graph_state", [False, True])
def test_non_graph_turn_keeps_previous_internal_context_and_appends_receipt(
    runtime_services, empty_graph_state
):
    """A读取后B纯审批/提前失败，C继续必须仍看见A的工具结果与B的真实事实。"""
    module, context, first = setup_run(runtime_services)
    continuation = importlib.import_module("knowledgeops_agent.continuation")
    state = {
        "messages": [
            HumanMessage("先读取关键条件", id="original-user"),
            AIMessage("", tool_calls=[{"name": "read_source", "args": {}, "id": "read-one"}]),
            ToolMessage("原材料的边界条件和反例", tool_call_id="read-one"),
        ],
        "working_summary": "用户要求保留反例与边界条件。",
    }
    context.jobs.finish(
        first["id"], context.worker_id, continuation=continuation.from_state(context, state)
    )
    middle = context.jobs.enqueue(first["conversation_id"], "确认保存", request_key="approval")
    context.jobs.claim(context.worker_id, run_id=middle["id"], generation=1)
    context.jobs.finish(
        middle["id"],
        context.worker_id,
        status="failed" if empty_graph_state else "completed",
        outcome={
            "answer": "规则加载失败" if empty_graph_state else "已保存指定修订",
            "answer_origin": "application_receipt",
        },
        continuation=continuation.from_state(context, {}) if empty_graph_state else None,
    )
    last = context.jobs.enqueue(first["conversation_id"], "继续解释反例", request_key="continue")
    context.jobs.claim(context.worker_id, run_id=last["id"], generation=1)
    snapshot = context.jobs.continuation(last["id"])
    messages = module.initial_messages(replace(context, run_id=last["id"], root_id=last["id"]))
    assert snapshot["content"]["summary"] == state["working_summary"]
    assert any(
        isinstance(m, ToolMessage) and m.content == "原材料的边界条件和反例" for m in messages
    )
    assert sum(m.content == "确认保存" for m in messages) == 1
    receipt = "规则加载失败" if empty_graph_state else "已保存指定修订"
    assert any(receipt in str(m.content) and not isinstance(m, AIMessage) for m in messages)
    assert messages[-1].content == "继续解释反例"
