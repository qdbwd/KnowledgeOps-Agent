"""独立审批请求的持久协议；真实PG/网关/预算，供应商为显式替身，不证明语义质量。"""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.app.runs.service import RunStopped
from knowledgeops_agent.harness.permissions.review import ActionReviewer
from knowledgeops_agent.persistence.models import ExecutionAction, ExecutionRule

runtime_services = _runtime_services


class Reviewer:
    def __init__(self, response=None, callback=None):
        self.response = response or AIMessage('{"verdict":"approve","reason":"已明确授权这一次操作"}')
        self.callback = callback
        self.requests = []

    def invoke(self, messages, **kwargs):
        self.requests.append((messages, kwargs))
        assert not kwargs.get("tools")
        if self.callback:
            self.callback()
        return self.response


def prepare(runtime_services, tmp_path, *, tool="execute", budgets=None):
    _, context, run = setup_run(runtime_services, task="请在工作区创建本次报告", budgets=budgets)
    context.workspace_root = tmp_path / "workspace"
    provider = Reviewer()
    context.gateway.client = provider
    arguments = {"command": "touch report.md"} if tool == "execute" else {"file_path": "/report.md", "content": "报告"}
    with context.jobs.sessions.begin() as session:
        action = ExecutionAction(
            root_id=run["id"], run_id=run["id"], tool_call_id="specific-call", tool_name=tool,
            arguments=arguments, fingerprint="f" * 64, workspace=str(context.workspace_root),
            cwd=str(context.workspace_root), authority_epoch=0, batch_key="b" * 64,
            position=0, origin="review", status="pending", decision={},
        )
        session.add(action)
        session.flush()
        identity = action.id
    facts = {"decision": "review", "reason": "本次操作需要审批", "mode": "auto_review",
             "settings_revision": 1, "rules": [], "target": {"path": "report.md", "hash": None}}
    return context, run, identity, provider, facts


@pytest.mark.parametrize("tool", ["execute", "write_file", "edit_file", "delete", "mcp_server_write"])
def test_one_specific_review_is_reused_without_running_tools_or_creating_rules(runtime_services, tmp_path, tool):
    context, run, action, provider, facts = prepare(runtime_services, tmp_path, tool=tool)
    reviewer = ActionReviewer(context)
    first = reviewer.review(action, current_facts=lambda session: facts)
    assert first.verdict == "approve"
    assert reviewer.review(action, current_facts=lambda session: facts) == first
    assert len(provider.requests) == 1
    records = context.gateway.ledger.records(root_id=run["id"])
    assert len(records) == 1 and records[0]["purpose"] == "execution_review"
    payload = json.loads(provider.requests[0][0][-1].content)
    assert payload["user_authorization"]["task"] == run["task"]
    assert payload["action"]["name"] == tool
    assert not context.workspace_root.exists()
    with context.jobs.sessions() as session:
        assert session.query(ExecutionRule).count() == 0
        saved = session.get(ExecutionAction, action)
        assert saved.status == "approved" and saved.decision["actor"] == "model"
        assert saved.decision["review"]["result"]["verdict"] == "approve"


@pytest.mark.parametrize("response", [
    AIMessage("not JSON"),
    AIMessage('{"verdict":"approve","reason":"ok","execute":"rm -rf /"}'),
    AIMessage('{"verdict":"approve","reason":"  "}'),
    AIMessage('{"verdict":"approve","reason":"ok"}', response_metadata={"finish_reason": "length"}),
    AIMessage("", tool_calls=[{"id": "x", "name": "execute", "args": {"command": "echo x"}}]),
])
def test_invalid_output_falls_back_to_human_and_cannot_retry_implicitly(runtime_services, tmp_path, response):
    context, _, action, provider, facts = prepare(runtime_services, tmp_path)
    provider.response = response
    reviewer = ActionReviewer(context)
    result = reviewer.review(action, current_facts=lambda session: facts)
    assert result.verdict == "ask" and result.code == "invalid_result"
    provider.response = Reviewer().response
    assert reviewer.review(action, current_facts=lambda session: facts) == result
    assert len(provider.requests) == 1
    with context.jobs.sessions() as session:
        assert session.get(ExecutionAction, action).status == "pending"


@pytest.mark.parametrize("decision", ["deny", "ask", "allow"])
def test_fixed_policy_is_decided_without_calling_the_reviewer(runtime_services, tmp_path, decision):
    context, _, action, provider, facts = prepare(runtime_services, tmp_path)
    facts["decision"] = decision
    result = ActionReviewer(context).review(action, current_facts=lambda session: facts)
    assert result.verdict == {"deny": "deny", "ask": "ask", "allow": "approve"}[decision]
    assert not provider.requests
    with context.jobs.sessions() as session:
        row = session.get(ExecutionAction, action)
        assert row.status == {"deny": "denied", "ask": "pending", "allow": "approved"}[decision]
        assert row.decision["review"]["request_started"] is False
        assert "call_id" not in row.decision["review"]
    facts["decision"] = "review"
    result = ActionReviewer(context).review(action, current_facts=lambda session: facts)
    assert result.verdict == "ask" and result.code == "binding_changed"
    assert not provider.requests


@pytest.mark.parametrize("failure", ["disabled", "timeout", "budget", "too_large"])
def test_unavailable_review_preserves_human_path_without_extra_budget(runtime_services, tmp_path, failure):
    context, run, action, provider, facts = prepare(runtime_services, tmp_path, budgets={"model_requests": 1})
    if failure == "disabled":
        context.gateway.enabled = False
    elif failure == "timeout":
        def timeout():
            raise TimeoutError("provider unavailable")
        provider.callback = timeout
    elif failure == "budget":
        context.gateway.invoke([HumanMessage("已经使用预算")], root_id=run["id"], run_id=run["id"],
                               root_limit=1, call_id="already-spent")
    else:
        facts["large_body"] = "x" * 100000
    before = len(provider.requests)
    result = ActionReviewer(context).review(action, current_facts=lambda session: facts)
    assert result.verdict == "ask"
    assert len(provider.requests) == before + (failure == "timeout")
    assert context.jobs.get_run(run["id"])["budgets"]["model_requests"] == 1


@pytest.mark.parametrize("when", ["during", "after"])
def test_changed_target_or_policy_cannot_reuse_an_approval(runtime_services, tmp_path, when):
    context, _, action, provider, facts = prepare(runtime_services, tmp_path)
    reviewer = ActionReviewer(context)
    def change():
        facts["target"] = {"path": "report.md", "hash": "new-file-content"}
        facts["settings_revision"] += 1
    if when == "during":
        provider.callback = change
    else:
        assert reviewer.review(action, current_facts=lambda session: facts).verdict == "approve"
        change()
    result = reviewer.review(action, current_facts=lambda session: facts)
    assert result.verdict == "ask" and result.code == "binding_changed"
    assert len(provider.requests) == 1
    with context.jobs.sessions() as session:
        assert session.get(ExecutionAction, action).status == "pending"


def test_cancellation_during_review_does_not_apply_late_approval(runtime_services, tmp_path):
    context, run, action, provider, facts = prepare(runtime_services, tmp_path)
    provider.callback = lambda: context.jobs.cancel(run["id"])
    with pytest.raises(RunStopped):
        ActionReviewer(context).review(action, current_facts=lambda session: facts)
    with context.jobs.sessions() as session:
        assert session.get(ExecutionAction, action).status != "approved"
    assert len(provider.requests) == 1


def test_completed_provider_receipt_survives_process_loss_before_business_commit(runtime_services, tmp_path, monkeypatch):
    context, run, action, provider, facts = prepare(runtime_services, tmp_path)
    reviewer = ActionReviewer(context)
    original = reviewer._finish
    def crash(*args, **kwargs):
        raise SystemExit("crash after model response was saved")
    monkeypatch.setattr(reviewer, "_finish", crash)
    with pytest.raises(SystemExit):
        reviewer.review(action, current_facts=lambda session: facts)
    context.jobs.recover_on_startup()
    context.jobs.resume(run["id"])
    current = context.jobs.get_run(run["id"])
    context.jobs.claim(context.worker_id, run_id=run["id"], generation=current["generation"])
    monkeypatch.setattr(reviewer, "_finish", original)
    assert reviewer.review(action, current_facts=lambda session: facts).verdict == "approve"
    assert len(provider.requests) == 1


def test_unknown_provider_receipt_is_never_resent(runtime_services, tmp_path):
    context, run, action, provider, facts = prepare(runtime_services, tmp_path)
    def crash():
        raise SystemExit("lost provider response")
    provider.callback = crash
    reviewer = ActionReviewer(context)
    with pytest.raises(SystemExit):
        reviewer.review(action, current_facts=lambda session: facts)
    context.jobs.recover_on_startup()
    context.jobs.resume(run["id"])
    current = context.jobs.get_run(run["id"])
    context.jobs.claim(context.worker_id, run_id=run["id"], generation=current["generation"])
    provider.callback = None
    assert reviewer.review(action, current_facts=lambda session: facts).verdict == "ask"
    assert len(provider.requests) == 1


@pytest.mark.parametrize("policy", ["deny", "ask"])
def test_new_fixed_policy_clears_a_previous_model_approval(runtime_services, tmp_path, policy):
    context, _, action, provider, facts = prepare(runtime_services, tmp_path)
    reviewer = ActionReviewer(context)
    assert reviewer.review(action, current_facts=lambda session: facts).verdict == "approve"
    facts["decision"] = policy
    assert reviewer.review(action, current_facts=lambda session: facts).verdict == policy
    with context.jobs.sessions() as session:
        assert session.get(ExecutionAction, action).status == {"deny": "denied", "ask": "pending"}[policy]
    assert len(provider.requests) == 1


def test_pause_after_response_preserves_receipt_and_requires_explicit_resume(runtime_services, tmp_path):
    context, run, action, provider, facts = prepare(runtime_services, tmp_path)
    provider.callback = lambda: context.jobs.pause(run["id"])
    reviewer = ActionReviewer(context)
    with pytest.raises(RunStopped):
        reviewer.review(action, current_facts=lambda session: facts)
    context.jobs.settle_pause(run["id"], context.worker_id, outcome={})
    with context.jobs.sessions() as session:
        assert session.get(ExecutionAction, action).status == "reviewing"
    provider.callback = None
    context.jobs.resume(run["id"])
    current = context.jobs.get_run(run["id"])
    context.jobs.claim(context.worker_id, run_id=run["id"], generation=current["generation"])
    assert reviewer.review(action, current_facts=lambda session: facts).verdict == "approve"
    assert len(provider.requests) == 1


def test_duplicate_inflight_review_never_resends_or_overwrites_human_fallback(runtime_services, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    context, _, action, provider, facts = prepare(runtime_services, tmp_path)
    started, release = Event(), Event()
    def wait_response():
        started.set()
        assert release.wait(10)
    provider.callback = wait_response
    reviewer = ActionReviewer(context)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(reviewer.review, action, current_facts=lambda session: facts)
        assert started.wait(10)
        try:
            second = reviewer.review(action, current_facts=lambda session: facts)
            assert second.verdict == "ask"
        finally:
            release.set()
        assert first.result(10) == second
    assert len(provider.requests) == 1


def test_secret_redaction_does_not_make_distinct_actions_share_approval(runtime_services, tmp_path):
    context, _, action, provider, facts = prepare(runtime_services, tmp_path)
    context.gateway.sensitive_values = ["test-secret-one", "test-secret-two"]
    with context.jobs.sessions.begin() as session:
        session.get(ExecutionAction, action).arguments = {"command": "some-tool test-secret-one"}
    reviewer = ActionReviewer(context)
    assert reviewer.review(action, current_facts=lambda session: facts).verdict == "approve"
    assert "test-secret-one" not in provider.requests[0][0][-1].content
    with context.jobs.sessions.begin() as session:
        session.get(ExecutionAction, action).arguments = {"command": "some-tool test-secret-two"}
    assert reviewer.review(action, current_facts=lambda session: facts).code == "binding_changed"
    assert len(provider.requests) == 1
