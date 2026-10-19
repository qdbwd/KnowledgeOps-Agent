"""真实 create_agent/HITL/执行工具闭环，供应商仅产生可重复的工具提案。"""

import json

from fixtures.execution_provider import ExecutionProvider
from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_api_worker import client as _client
from test_api_worker import services as _services
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run
from test_workspace_runtime import enable

from knowledgeops_agent.harness.permissions.approvals import ExecutionApprovals
from knowledgeops_agent.harness.permissions.settings import ExecutionSettings
from knowledgeops_agent.harness.tools.policy import ToolPolicyService

runtime_services = _runtime_services
client = _client
services = _services


class Commands:
    invoke = ExecutionProvider.invoke

    def __init__(self, commands):
        self.commands = commands

    def bind_tools(self, tools):
        return self

    def stream(self, messages, **kwargs):
        if any(isinstance(message, ToolMessage) for message in messages):
            yield AIMessageChunk("处理完毕。")
        else:
            yield AIMessageChunk(
                "",
                tool_call_chunks=[
                    {
                        "name": "execute",
                        "args": json.dumps({"command": command}),
                        "id": f"command-{i}",
                        "index": i,
                    }
                    for i, command in enumerate(self.commands)
                ],
            )


def graph_setup(runtime_services, tmp_path, commands):
    module, context, run = setup_run(runtime_services)
    context.workspace_root = tmp_path / "workspace"
    enable(context.jobs.sessions)
    context.gateway.client = Commands(commands)
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": run["checkpoint_id"]}}
    return module, context, run, graph, config


def save_wait(context, run, graph, config):
    approvals = ExecutionApprovals(context)
    snapshot = graph.get_state(config)
    assert snapshot.interrupts
    approvals.record_interrupts(snapshot)
    context.jobs.finish(
        run["id"], context.worker_id, status="interrupted", outcome={"kind": "waiting_approval"}
    )
    return approvals, snapshot


def resume_graph(context, graph, config):
    run = context.jobs.get_run(context.root_id)
    assert run["status"] == "queued"
    context.jobs.claim(context.worker_id, run_id=run["id"], generation=run["generation"])
    inputs = ExecutionApprovals(context).resume_input(graph.get_state(config))
    return graph.invoke(inputs, config, context=context)


def test_batch_waits_before_side_effect_and_resumes_original_decisions(runtime_services, tmp_path):
    module, context, run, graph, config = graph_setup(
        runtime_services, tmp_path, ["touch work/first", "touch work/second"]
    )
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    approvals, _ = save_wait(context, run, graph, config)
    rows = approvals.list()
    assert len(rows) == 2
    assert not (context.workspace_root / "work/first").exists()
    assert context.jobs.get_run(run["id"])["tool_calls"] == 0
    kwargs = {"expected_fingerprint": rows[0]["fingerprint"], "approve": True, "scope": "session"}
    approvals.decide(rows[0]["id"], **kwargs)
    assert context.jobs.get_run(run["id"])["status"] == "interrupted"
    approvals.decide(rows[0]["id"], **kwargs)
    approvals.decide(rows[1]["id"], expected_fingerprint=rows[1]["fingerprint"], approve=False)
    result = resume_graph(context, graph, config)
    assert (context.workspace_root / "work/first").exists()
    assert not (context.workspace_root / "work/second").exists()
    assert any(
        isinstance(m, ToolMessage) and "reject" in m.content.lower() for m in result["messages"]
    )
    assert context.jobs.get_run(run["id"])["tool_calls"] == 1
    assert not graph.get_state(config).interrupts
    assert (
        len(
            ExecutionSettings(context.jobs.sessions).rules(
                str(context.workspace_root), run["conversation_id"]
            )
        )
        == 1
    )


def test_permission_change_at_tools_boundary_still_interrupts_old_checkpoint(
    runtime_services, tmp_path
):
    module, context, run, graph, config = graph_setup(
        runtime_services, tmp_path, ["touch work/fallback"]
    )
    settings = ExecutionSettings(context.jobs.sessions)
    settings.update(run["conversation_id"], expected_revision=0, permission_mode="auto_review")
    graph.invoke(
        {"messages": module.initial_messages(context)},
        config,
        context=context,
        interrupt_before=["tools"],
    )
    assert graph.get_state(config).next == ("tools",)
    settings.update(run["conversation_id"], expected_revision=1, permission_mode="ask")
    graph.invoke(None, config, context=context)
    approvals, snapshot = save_wait(context, run, graph, config)
    assert "execution_action_id" in snapshot.interrupts[0].value
    assert not (context.workspace_root / "work/fallback").exists()
    row = approvals.list()[0]
    approvals.decide(row["id"], expected_fingerprint=row["fingerprint"], approve=True)
    resume_graph(context, graph, config)
    assert (context.workspace_root / "work/fallback").exists()


def test_tool_revocation_after_approval_blocks_execution(runtime_services, tmp_path):
    module, context, run, graph, config = graph_setup(
        runtime_services, tmp_path, ["touch work/forbidden"]
    )
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    approvals, _ = save_wait(context, run, graph, config)
    row = approvals.list()[0]
    approvals.decide(row["id"], expected_fingerprint=row["fingerprint"], approve=True)
    policy = ToolPolicyService(context.jobs.sessions)
    policy.update(expected_revision=policy.get()["revision"], enabled={"middleware:execute": False})
    resume_graph(context, graph, config)
    assert not (context.workspace_root / "work/forbidden").exists()
    assert context.jobs.get_run(run["id"])["tool_calls"] == 0


def test_cancel_waiting_run_cannot_be_revived_by_late_approval(runtime_services, tmp_path):
    import pytest

    module, context, run, graph, config = graph_setup(runtime_services, tmp_path, ["touch work/no"])
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    approvals, _ = save_wait(context, run, graph, config)
    row = approvals.list()[0]
    assert context.jobs.cancel(run["id"])["status"] == "cancelled"
    with pytest.raises(PermissionError):
        approvals.decide(row["id"], expected_fingerprint=row["fingerprint"], approve=True)
    assert not (context.workspace_root / "work/no").exists()
    # 原有“显式恢复取消任务”仍可用，但恢复不能复活已取消的命令提议。
    context.jobs.resume(run["id"])
    result = resume_graph(context, graph, config)
    assert result["messages"][-1].text == "处理完毕。"
    assert not (context.workspace_root / "work/no").exists()


def test_queued_message_waits_but_explicit_insertion_invalidates_pending_action(
    runtime_services, tmp_path
):
    from uuid import uuid4

    module, context, run, graph, config = graph_setup(
        runtime_services, tmp_path, ["touch work/old"]
    )
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    approvals, _ = save_wait(context, run, graph, config)
    queued = context.jobs.enqueue(
        run["conversation_id"], "不要执行，改为说明情况", request_key=str(uuid4())
    )
    assert context.jobs.claim("other", run_id=queued["id"], generation=1) is None
    context.jobs.guide(queued["id"], target_id=run["id"], target_generation=1)
    assert approvals.list()[0]["status"] == "invalidated"
    assert context.jobs.get_run(run["id"])["status"] == "queued"
    assert context.jobs.guidance(run["id"])[0]["target_generation"] == 2
    assert (
        context.jobs.guide(queued["id"], target_id=run["id"], target_generation=1)[
            "target_generation"
        ]
        == 2
    )
    inputs = approvals.resume_input(graph.get_state(config))
    assert inputs.resume[next(iter(inputs.resume))]["decisions"][0]["type"] == "reject"
    assert not (context.workspace_root / "work/old").exists()


def test_deleted_rule_revokes_not_yet_executed_grant(runtime_services, tmp_path):
    module, context, run, graph, config = graph_setup(runtime_services, tmp_path, ["touch work/no"])
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    approvals, _ = save_wait(context, run, graph, config)
    row = approvals.list()[0]
    approvals.decide(
        row["id"], expected_fingerprint=row["fingerprint"], approve=True, scope="persistent"
    )
    settings = ExecutionSettings(context.jobs.sessions)
    rule = settings.rules(str(context.workspace_root))[0]
    settings.delete_rule(rule["id"], workspace=str(context.workspace_root), expected_revision=1)
    resume_graph(context, graph, config)
    assert not (context.workspace_root / "work/no").exists()


def test_restart_after_approval_requires_manual_resume_then_uses_original_grant(
    runtime_services, tmp_path
):
    module, context, run, graph, config = graph_setup(
        runtime_services, tmp_path, ["touch work/restart"]
    )
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    approvals, _ = save_wait(context, run, graph, config)
    row = approvals.list()[0]
    approvals.decide(row["id"], expected_fingerprint=row["fingerprint"], approve=True)
    assert context.jobs.get_run(run["id"])["status"] == "queued"
    context.jobs.recover_on_startup()
    recovered = context.jobs.get_run(run["id"])
    assert recovered["status"] == "interrupted" and recovered["outcome"]["kind"] == "partial"
    assert context.jobs.claim(context.worker_id, run_id=run["id"], generation=2) is None
    context.jobs.resume(run["id"])
    resume_graph(context, graph, config)
    assert (context.workspace_root / "work/restart").exists()
    assert context.jobs.get_run(run["id"])["tool_calls"] == 1


def test_lost_long_result_after_real_execution_is_unknown(runtime_services, tmp_path, monkeypatch):
    import pytest

    from knowledgeops_agent.harness.permissions.receipts import CommandJournal, ExecutionUnknown

    module, context, run, graph, config = graph_setup(
        runtime_services, tmp_path, ["printf x >> work/proof"]
    )
    ExecutionSettings(context.jobs.sessions).update(
        run["conversation_id"], expected_revision=0, permission_mode="auto_review"
    )

    original = module.RuntimeControlMiddleware._deliver_tool_message
    fail = [True]

    def broken_delivery(*args):
        if fail[0]:
            raise OSError("injected result storage failure")
        return original(*args)

    monkeypatch.setattr(
        module.RuntimeControlMiddleware, "_deliver_tool_message", staticmethod(broken_delivery)
    )
    with pytest.raises(ExecutionUnknown):
        graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    assert (context.workspace_root / "work/proof").read_text() == "x"
    assert CommandJournal(context).list()[0]["status"] == "unknown"
    context.jobs.finish(
        run["id"], context.worker_id, status="interrupted", outcome={"kind": "execution_unknown"}
    )
    context.jobs.cancel(run["id"])
    context.jobs.resume(run["id"])
    ExecutionSettings(context.jobs.sessions).update(
        run["conversation_id"], expected_revision=1, permission_mode="ask"
    )
    receipt = CommandJournal(context).list()[0]
    CommandJournal(context).resolve(
        receipt["id"], expected_revision=receipt["revision"], decision="retry"
    )
    from knowledgeops_agent.harness.permissions.approvals import queue_after_decisions

    queue_after_decisions(context.jobs, run["id"])
    fail[0] = False
    result = resume_graph(context, graph, config)
    assert result["messages"][-1].text == "处理完毕。"
    assert (context.workspace_root / "work/proof").read_text() == "xx"
    assert context.jobs.get_run(run["id"])["tool_calls"] == 2
    assert CommandJournal(context).list()[0]["attempt"] == 2


def test_worker_classifies_native_interrupt_and_http_approval_queues_once(
    services, client, tmp_path
):
    from uuid import uuid4

    from test_runtime import DraftProvider

    from knowledgeops_agent.app.runs.worker import execute_run

    class Provider(Commands):
        def invoke(self, messages, **kwargs):
            return DraftProvider("unused").invoke(messages, **kwargs)

    services.gateway.enabled = True
    services.gateway.client = Provider(["touch work/from-worker"])
    services.settings.workspace_root = tmp_path / "workspace"
    enable(services.sessions)
    conv = client.post("/api/conversations", json={}).json()["id"]
    run = client.post(
        f"/api/conversations/{conv}/messages",
        json={"content": "在工作区生成文件", "request_key": str(uuid4())},
    ).json()
    saver = InMemorySaver()
    result = execute_run(
        run["id"], 1, services=services, worker_id="worker-api", checkpointer=saver
    )
    assert result["status"] == "interrupted" and result["outcome"]["kind"] == "waiting_approval"
    assert result["tool_calls"] == 0
    row = client.get(f"/api/runs/{run['id']}/approvals").json()["actions"][0]
    url = f"/api/runs/{run['id']}/approvals/{row['id']}/decision"
    payload = {"expected_fingerprint": row["fingerprint"], "approve": True}
    assert client.post(url, json=payload).status_code == 200
    assert client.post(url, json=payload).status_code == 200
    resumed = services.jobs.get_run(run["id"])
    assert resumed["generation"] == 2
    result = execute_run(
        run["id"], 2, services=services, worker_id="worker-api", checkpointer=saver
    )
    assert result["status"] == "completed", result
    assert (services.settings.workspace_root / "work/from-worker").exists()
    assert result["tool_calls"] == 1
    assert (
        execute_run(run["id"], 2, services=services, worker_id="worker-api", checkpointer=saver)
        is None
    )
