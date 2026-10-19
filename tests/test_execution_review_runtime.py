"""命令审查走真实create_agent/HITL/工具边界；独立模型仅替换供应商响应。"""

import json

import pytest
from langchain_core.messages import AIMessage
from test_execution_harness import Commands, graph_setup, resume_graph, save_wait
from test_runtime import runtime_services as _runtime_services

from knowledgeops_agent.harness.permissions.approvals import ExecutionApprovals
from knowledgeops_agent.harness.permissions.settings import ExecutionSettings

runtime_services = _runtime_services


class ReviewedCommands(Commands):
    def __init__(self, commands, verdict="approve"):
        super().__init__(commands)
        self.verdict = verdict
        self.reviews = []

    def invoke(self, messages, **kwargs):
        payload = json.loads(messages[-1].content)
        assert payload["action"]["name"] == "execute"
        self.reviews.append(payload)
        return AIMessage(json.dumps({"verdict": self.verdict, "reason": "固定验收判定"}))


def setup(runtime_services, tmp_path, verdict="approve"):
    values = graph_setup(runtime_services, tmp_path, ["touch work/reviewed"])
    _, context, run, _, _ = values
    context.gateway.client = ReviewedCommands(["touch work/reviewed"], verdict)
    ExecutionSettings(context.jobs.sessions).update(run["conversation_id"], expected_revision=0,
                                                   permission_mode="auto_review")
    return values


def test_review_approval_runs_exact_command_and_reuses_review_at_tool_boundary(runtime_services, tmp_path):
    module, context, run, graph, config = setup(runtime_services, tmp_path)
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    assert (context.workspace_root / "work/reviewed").exists()
    assert not graph.get_state(config).interrupts
    assert len(context.gateway.client.reviews) == 1
    actions = ExecutionApprovals(context).list()
    assert len(actions) == 1 and actions[0]["decision"]["actor"] == "model"
    assert context.jobs.get_run(run["id"])["tool_calls"] == 1


@pytest.mark.parametrize("approve", [False, True])
def test_review_denial_waits_for_user_and_never_grants_a_reusable_rule(runtime_services, tmp_path, approve):
    module, context, run, graph, config = setup(runtime_services, tmp_path, "deny")
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    assert not (context.workspace_root / "work/reviewed").exists()
    approvals, snapshot = save_wait(context, run, graph, config)
    assert snapshot.interrupts
    row = approvals.list()[0]
    assert row["decision"]["review"]["result"]["verdict"] == "deny"
    assert len(context.gateway.client.reviews) == 1
    assert context.jobs.get_run(run["id"])["tool_calls"] == 0
    with pytest.raises(ValueError, match="一次"):
        approvals.decide(row["id"], expected_fingerprint=row["fingerprint"], approve=True, scope="persistent")
    approvals.decide(row["id"], expected_fingerprint=row["fingerprint"], approve=approve)
    resume_graph(context, graph, config)
    assert (context.workspace_root / "work/reviewed").exists() == approve
    assert len(context.gateway.client.reviews) == 1


def test_review_asks_a_human_then_resumes_without_another_review(runtime_services, tmp_path):
    module, context, run, graph, config = setup(runtime_services, tmp_path, "ask")
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    approvals, _ = save_wait(context, run, graph, config)
    assert not (context.workspace_root / "work/reviewed").exists()
    action = approvals.list()[0]
    assert action["status"] == "pending" and action["decision"]["review"]["result"]["verdict"] == "ask"
    approvals.decide(action["id"], expected_fingerprint=action["fingerprint"], approve=True)
    resume_graph(context, graph, config)
    assert (context.workspace_root / "work/reviewed").exists()
    assert len(context.gateway.client.reviews) == 1


def test_change_to_human_mode_after_review_invalidates_model_grant(runtime_services, tmp_path):
    module, context, run, graph, config = setup(runtime_services, tmp_path)
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context,
                 interrupt_before=["tools"])
    assert not (context.workspace_root / "work/reviewed").exists()
    ExecutionSettings(context.jobs.sessions).update(run["conversation_id"], expected_revision=1,
                                                   permission_mode="ask")
    graph.invoke(None, config, context=context)
    approvals, snapshot = save_wait(context, run, graph, config)
    assert "execution_action_id" in snapshot.interrupts[0].value
    row = approvals.list()[0]
    assert row["status"] == "pending"
    approvals.decide(row["id"], expected_fingerprint=row["fingerprint"], approve=True)
    resume_graph(context, graph, config)
    assert (context.workspace_root / "work/reviewed").exists()
    assert len(context.gateway.client.reviews) == 1


@pytest.mark.parametrize("effect", ["allow", "ask", "deny"])
def test_explicit_command_rule_avoids_the_review_request(runtime_services, tmp_path, effect):
    module, context, run, graph, config = setup(runtime_services, tmp_path)
    settings = ExecutionSettings(context.jobs.sessions)
    settings.create_rule(workspace=str(context.workspace_root), conversation_id=run["conversation_id"],
                         argv=["touch", "work/reviewed"], cwd=str(context.workspace_root),
                         match="exact", effect=effect)
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    assert (context.workspace_root / "work/reviewed").exists() == (effect == "allow")
    assert bool(graph.get_state(config).interrupts) == (effect == "ask")
    assert not context.gateway.client.reviews


def test_one_human_fallback_stops_other_reviewed_commands_in_the_same_batch(runtime_services, tmp_path):
    commands = ["touch work/first", "touch work/second"]
    module, context, run, graph, config = graph_setup(runtime_services, tmp_path, commands)
    class MixedReviewer(ReviewedCommands):
        def invoke(self, messages, **kwargs):
            payload = json.loads(messages[-1].content)
            self.verdict = "ask" if "second" in payload["action"]["arguments"]["command"] else "approve"
            return super().invoke(messages, **kwargs)
    context.gateway.client = MixedReviewer(commands)
    ExecutionSettings(context.jobs.sessions).update(run["conversation_id"], expected_revision=0,
                                                   permission_mode="auto_review")
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    approvals, _ = save_wait(context, run, graph, config)
    assert not (context.workspace_root / "work/first").exists()
    assert not (context.workspace_root / "work/second").exists()
    pending = next(row for row in approvals.list() if row["status"] == "pending")
    approvals.decide(pending["id"], expected_fingerprint=pending["fingerprint"], approve=True)
    resume_graph(context, graph, config)
    assert (context.workspace_root / "work/first").exists()
    assert (context.workspace_root / "work/second").exists()
    assert len(context.gateway.client.reviews) == 2
    assert context.jobs.get_run(run["id"])["tool_calls"] == 2


@pytest.mark.parametrize("change", ["human_mode", "deny_rule", "disable_tool"])
def test_command_start_rechecks_authorization_after_the_tool_gate(runtime_services, tmp_path, monkeypatch, change):
    from knowledgeops_agent.harness.permissions.approvals import ApprovalWaiting
    from knowledgeops_agent.harness.permissions.receipts import CommandJournal
    from knowledgeops_agent.harness.tools.policy import ToolPolicyService

    module, context, run, graph, config = setup(runtime_services, tmp_path)
    original = CommandJournal.start
    changed = False

    def at_start(self, call, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            settings = ExecutionSettings(context.jobs.sessions)
            if change == "human_mode":
                settings.update(run["conversation_id"], expected_revision=1, permission_mode="ask")
            elif change == "deny_rule":
                settings.create_rule(workspace=str(context.workspace_root), conversation_id=run["conversation_id"],
                                     argv=["touch", "work/reviewed"], cwd=str(context.workspace_root),
                                     match="exact", effect="deny")
            else:
                policy = ToolPolicyService(context.jobs.sessions)
                policy.update(expected_revision=policy.get()["revision"], enabled={"middleware:execute": False})
        return original(self, call, **kwargs)

    monkeypatch.setattr(CommandJournal, "start", at_start)
    if change == "human_mode":
        with pytest.raises(ApprovalWaiting):
            graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    else:
        graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    assert not (context.workspace_root / "work/reviewed").exists()
    assert context.jobs.get_run(run["id"])["tool_calls"] == 0
    assert len(context.gateway.client.reviews) == 1
    assert CommandJournal(context).list() == []
    if change == "human_mode":
        context.jobs.finish(run["id"], context.worker_id, status="interrupted", outcome={"kind": "waiting_approval"})
        approvals = ExecutionApprovals(context)
        row = approvals.list()[0]
        assert row["status"] == "pending"
        approvals.decide(row["id"], expected_fingerprint=row["fingerprint"], approve=True)
        resume_graph(context, graph, config)
        assert (context.workspace_root / "work/reviewed").exists()
        assert context.jobs.get_run(run["id"])["tool_calls"] == 1
        assert len(context.gateway.client.reviews) == 1
