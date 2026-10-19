"""真实MCP调用链的统一审批；供应商替身不代替本地协议服务。"""

import json

import pytest
from langchain_core.messages import AIMessage
from test_mcp_business import configured, registry
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.harness.mcp.calls import MCPApprovalRequired, MCPCalls
from knowledgeops_agent.harness.permissions.settings import ExecutionSettings
from knowledgeops_agent.harness.runtime.resources import prepare_resources

runtime_services = _runtime_services


class Reviewer:
    def __init__(self, verdict="approve", callback=None):
        self.verdict, self.callback, self.requests = verdict, callback, []

    def invoke(self, messages, **kwargs):
        self.requests.append(json.loads(messages[-1].content))
        if self.callback:
            self.callback()
        return AIMessage(json.dumps({"verdict": self.verdict, "reason": "固定MCP审查判定"}))


def setup(runtime_services, verdict="approve"):
    _, context, run = setup_run(runtime_services)
    configured(context, "confirm")
    prepare_resources(context)
    context.gateway.client = Reviewer(verdict)
    ExecutionSettings(context.jobs.sessions).update(run["conversation_id"], expected_revision=0,
                                                   permission_mode="auto_review")
    return context, run, MCPCalls(context), context.resources["mcp"][0]


def test_model_approval_executes_mcp_once_and_persists_exact_service_facts(runtime_services):
    context, run, calls, binding = setup(runtime_services)
    result = calls.invoke(binding, "get_note", {"topic": "审批后的真实协议调用"}, call_id="review-mcp")
    assert result["status"] == "succeeded"
    assert len(context.gateway.client.requests) == 1
    snapshot = context.gateway.client.requests[0]
    assert snapshot["action"]["arguments"] == {"topic": "审批后的真实协议调用"}
    assert snapshot["facts"]["service"]["id"] == binding["id"]
    assert snapshot["facts"]["tool"]["schema_hash"] == binding["tools"][0]["schema_hash"]
    receipt = calls.list()[0]
    assert receipt["decision"]["actor"] == "model"
    assert calls.invoke(binding, "get_note", {"topic": "审批后的真实协议调用"}, call_id="review-mcp") == result
    assert context.jobs.get_run(run["id"])["tool_calls"] == 1
    assert len(context.gateway.client.requests) == 1


@pytest.mark.parametrize("verdict", ["ask", "deny"])
def test_model_uncertainty_or_denial_waits_for_precise_human_decision(runtime_services, verdict):
    context, run, calls, binding = setup(runtime_services, verdict)
    with pytest.raises(MCPApprovalRequired):
        calls.invoke(binding, "get_note", {"topic": "人工核对"}, call_id="human")
    receipt = calls.list()[0]
    assert receipt["status"] == "pending"
    assert receipt["decision"]["review"]["result"]["verdict"] == verdict
    assert context.jobs.get_run(run["id"])["tool_calls"] == 0
    calls.decide(receipt["id"], expected_hash=receipt["input_hash"], approve=True)
    assert calls.invoke(binding, "get_note", {"topic": "人工核对"}, call_id="human")["status"] == "succeeded"
    assert len(context.gateway.client.requests) == 1
    assert calls.list()[0]["decision"]["actor"] == "user"


def test_revoking_connection_during_review_ends_without_external_call(runtime_services):
    context, run, calls, binding = setup(runtime_services)
    context.gateway.client.callback = lambda: registry(context).update(
        binding["id"], expected_revision=binding["revision"], enabled=False)
    result = calls.invoke(binding, "get_note", {"topic": "不应执行"}, call_id="revoke")
    assert result["status"] == "denied"
    assert context.jobs.get_run(run["id"])["tool_calls"] == 0
    assert calls.list()[0]["status"] == "denied"
    with pytest.raises((PermissionError, ValueError), match="撤回|决定"):
        receipt = calls.list()[0]
        calls.decide(receipt["id"], expected_hash=receipt["input_hash"], approve=True)


def test_completed_review_response_survives_restart_without_another_request(runtime_services, monkeypatch):
    from knowledgeops_agent.harness.permissions.review import ActionReviewer

    context, run, calls, binding = setup(runtime_services)
    def crash(*args, **kwargs):
        raise SystemExit("审查响应已保存，决定尚未落库")
    with monkeypatch.context() as patch:
        patch.setattr(ActionReviewer, "_finish", crash)
        with pytest.raises(SystemExit):
            calls.invoke(binding, "get_note", {"topic": "恢复审查"}, call_id="restart")
    assert calls.list()[0]["status"] == "reviewing"
    assert context.jobs.get_run(run["id"])["tool_calls"] == 0
    context.jobs.recover_on_startup()
    restored = context.jobs.resume(run["id"])
    context.jobs.claim(context.worker_id, run_id=run["id"], generation=restored["generation"])
    assert calls.invoke(binding, "get_note", {"topic": "恢复审查"}, call_id="restart")["status"] == "succeeded"
    assert len(context.gateway.client.requests) == 1


def test_cancellation_during_review_revokes_pending_call(runtime_services):
    from knowledgeops_agent.app.runs.service import RunStopped

    context, run, calls, binding = setup(runtime_services)
    context.gateway.client.callback = lambda: context.jobs.cancel(run["id"])
    with pytest.raises(RunStopped):
        calls.invoke(binding, "get_note", {"topic": "取消审查"}, call_id="cancel")
    assert calls.list()[0]["status"] == "denied"
    assert context.jobs.get_run(run["id"])["tool_calls"] == 0


def test_mode_change_while_connecting_waits_before_the_remote_operation(runtime_services, monkeypatch):
    from contextlib import asynccontextmanager

    context, run, calls, binding = setup(runtime_services)
    original = context.jobs.mcp_sessions.lease
    @asynccontextmanager
    async def changed(*args, **kwargs):
        async with original(*args, **kwargs) as lease:
            ExecutionSettings(context.jobs.sessions).update(run["conversation_id"], expected_revision=1,
                                                           permission_mode="ask")
            yield lease
    with monkeypatch.context() as patch:
        patch.setattr(context.jobs.mcp_sessions, "lease", changed)
        with pytest.raises(MCPApprovalRequired):
            calls.invoke(binding, "get_note", {"topic": "改为人工"}, call_id="changed-mode")
    assert calls.list()[0]["status"] == "pending"
    assert context.jobs.get_run(run["id"])["tool_calls"] == 0
    assert len(context.gateway.client.requests) == 1


def test_mode_change_after_review_is_checked_in_the_final_send_transaction(runtime_services, monkeypatch):
    import knowledgeops_agent.harness.mcp.calls as module

    context, run, calls, binding = setup(runtime_services)
    original = module.review_call
    checks = 0

    def changed(*args, **kwargs):
        nonlocal checks
        original(*args, **kwargs)
        checks += 1
        if checks == 2:
            ExecutionSettings(context.jobs.sessions).update(run["conversation_id"], expected_revision=1,
                                                           permission_mode="ask")

    monkeypatch.setattr(module, "review_call", changed)
    with pytest.raises(MCPApprovalRequired):
        calls.invoke(binding, "get_note", {"topic": "最后发送前改为人工"}, call_id="final-mode")
    assert checks == 2
    row = calls.list()[0]
    assert row["status"] == "pending"
    assert row["decision"]["review"]["result"]["code"] == "binding_changed"
    assert context.jobs.get_run(run["id"])["tool_calls"] == 0
    assert len(context.gateway.client.requests) == 1
