"""用实际文件副作用验证崩溃窗口和幂等扣额，不请求模型。"""

import pytest
from langchain_core.messages import ToolMessage
from test_execution_harness import graph_setup
from test_runtime import runtime_services as _runtime_services

from knowledgeops_agent.harness.permissions.approvals import ExecutionApprovals
from knowledgeops_agent.harness.permissions.receipts import CommandJournal, ExecutionUnknown
from knowledgeops_agent.harness.sandbox.execution import execute_local_command
from knowledgeops_agent.persistence.models import ExecutionAction

runtime_services = _runtime_services


@pytest.fixture
def journal(runtime_services, tmp_path):
    _, context, _, _, _ = graph_setup(runtime_services, tmp_path, [])
    return CommandJournal(context), context.jobs, context.workspace_root


def approved(service, call):
    """回执单元测试明确提供一次性人工批准；HTTP与图中断另有完整闭环测试。"""
    row = ExecutionApprovals(service.context)._ensure(call, {}, origin="review")
    with service.context.jobs.sessions.begin() as session:
        action = session.get(ExecutionAction, row["id"])
        if action.status == "pending":
            action.status = "approved"
            action.decision = {**action.decision, "actor": "user", "approve": True, "scope": "once"}


def invoke_once(journal, path, call):
    approved(journal, call)
    ticket = journal.start(call, workspace=str(path), cwd=str(path))
    if ticket["cached"]:
        return ticket["result"]
    result = execute_local_command(
        call["args"]["command"],
        cwd=path,
        env={"PATH": "/usr/bin:/bin"},
        timeout=5,
        guard=lambda: None,
    )
    message = ToolMessage(result.output, name="execute", tool_call_id=call["id"])
    journal.finish(ticket, message)
    return message


def test_replayed_success_does_not_execute_or_charge_twice(journal):
    service, jobs, path = journal
    call = {"id": "one", "name": "execute", "args": {"command": "printf x >> counter"}}
    invoke_once(service, path, call)
    invoke_once(service, path, call)
    assert (path / "counter").read_text() == "x"
    assert jobs.get_run(service.context.root_id)["tool_calls"] == 1
    with pytest.raises(PermissionError):
        service.start(
            {**call, "args": {"command": "touch other"}}, workspace=str(path), cwd=str(path)
        )


def test_crash_after_side_effect_is_unknown_and_only_explicit_retry_charges(journal):
    service, jobs, path = journal
    call = {"id": "crash", "name": "execute", "args": {"command": "printf x >> counter"}}
    approved(service, call)
    ticket = service.start(call, workspace=str(path), cwd=str(path))
    execute_local_command(call["args"]["command"], cwd=path, env={}, timeout=5, guard=lambda: None)
    # 不提交结果，相当于进程在实际写文件之后退出。
    with pytest.raises(ExecutionUnknown):
        service.start(call, workspace=str(path), cwd=str(path))
    assert (path / "counter").read_text() == "x"
    row = service.list()[0]
    assert row["status"] == "unknown"
    jobs.finish(
        service.context.root_id,
        "worker-test",
        status="interrupted",
        outcome={"kind": "execution_unknown"},
    )
    resolved = service.resolve(row["id"], expected_revision=row["revision"], decision="retry")
    assert resolved["status"] == "retry_ready"
    with pytest.raises(ValueError):
        service.resolve(row["id"], expected_revision=row["revision"], decision="retry")
    # 测试只重新领取原运行；生产由审批协调器写入持久调度。
    from knowledgeops_agent.persistence.models import Run

    with jobs.sessions.begin() as session:
        run = session.get(Run, service.context.root_id)
        run.status = "queued"
    jobs.claim("worker-test", run_id=service.context.root_id, generation=1)
    invoke_once(service, path, call)
    assert (path / "counter").read_text() == "xx"
    assert jobs.get_run(service.context.root_id)["tool_calls"] == 2
    assert ticket["attempt"] == 1


def test_unknown_skip_is_explicit_failure_not_fabricated_success(journal):
    service, jobs, path = journal
    call = {"id": "skip", "name": "execute", "args": {"command": "echo x"}}
    approved(service, call)
    service.start(call, workspace=str(path), cwd=str(path))
    with pytest.raises(ExecutionUnknown):
        service.start(call, workspace=str(path), cwd=str(path))
    row = service.list()[0]
    jobs.finish(
        service.context.root_id,
        "worker-test",
        status="interrupted",
        outcome={"kind": "execution_unknown"},
    )
    service.resolve(row["id"], expected_revision=row["revision"], decision="skip")
    assert service.list()[0]["result"]["status"] == "error"


def test_cancelled_unknown_can_reopen_reconciliation_but_cannot_execute_automatically(journal):
    service, jobs, path = journal
    call = {"id": "cancel-unknown", "name": "execute", "args": {"command": "touch x"}}
    approved(service, call)
    ticket = service.start(call, workspace=str(path), cwd=str(path))
    service.mark_unknown(ticket)
    jobs.finish(
        service.context.root_id,
        "worker-test",
        status="interrupted",
        outcome={"kind": "execution_unknown"},
    )
    assert jobs.cancel(service.context.root_id)["status"] == "cancelled"
    receipt = service.list()[0]
    with pytest.raises(PermissionError):
        service.resolve(receipt["id"], expected_revision=receipt["revision"], decision="retry")
    reopened = jobs.resume(service.context.root_id)
    assert (
        reopened["status"] == "interrupted" and reopened["outcome"]["kind"] == "execution_unknown"
    )
    assert (
        jobs.claim("worker-test", run_id=reopened["id"], generation=reopened["generation"]) is None
    )
    service.resolve(receipt["id"], expected_revision=receipt["revision"], decision="skip")
    assert service.list()[0]["status"] == "skipped"
