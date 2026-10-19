"""文件审批走真实图、SDK文件实现与PG回执；模型仅为明确的固定响应。"""

import json
import os

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select
from test_api_worker import client as _client
from test_api_worker import services as _services
from test_execution_harness import graph_setup, resume_graph, save_wait
from test_file_reliability import FileProvider
from test_runtime import runtime_services as _runtime_services

from knowledgeops_agent.harness.permissions.approvals import ExecutionApprovals
from knowledgeops_agent.harness.permissions.settings import ExecutionSettings
from knowledgeops_agent.persistence.models import FileOperation

runtime_services = _runtime_services
client = _client
services = _services


class ReviewedFiles(FileProvider):
    def __init__(self, batches, verdict="approve", during_review=None):
        super().__init__(batches)
        self.verdict, self.during_review = verdict, during_review
        self.reviews = []

    def invoke(self, messages, **kwargs):
        payload = json.loads(messages[-1].content)
        self.reviews.append(payload)
        if self.during_review:
            self.during_review()
        return AIMessage(json.dumps({"verdict": self.verdict, "reason": "固定文件审批验收"}))


def case(runtime_services, tmp_path, batches, *, mode="ask", **kwargs):
    values = graph_setup(runtime_services, tmp_path, [])
    _, context, run, _, _ = values
    context.gateway.client = ReviewedFiles(batches, **kwargs)
    if mode != "ask":
        ExecutionSettings(context.jobs.sessions).update(run["conversation_id"],
                                                       expected_revision=0, permission_mode=mode)
    return values


def test_human_file_approval_waits_for_the_whole_batch_then_executes_once(runtime_services, tmp_path):
    values = case(runtime_services, tmp_path, [[
        ("write_file", {"file_path": "/work/one.txt", "content": "one"}),
        ("write_file", {"file_path": "/work/two.txt", "content": "two"}),
    ]])
    module, context, run, graph, config = values
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    approvals, _ = save_wait(context, run, graph, config)
    rows = approvals.list()
    assert len(rows) == 2 and context.jobs.get_run(run["id"])["tool_calls"] == 0
    assert not (context.workspace_root / "work/one.txt").exists()
    assert rows[0]["decision"]["file_target"]["kind"] == "missing"
    approvals.decide(rows[0]["id"], expected_fingerprint=rows[0]["fingerprint"], approve=True)
    assert context.jobs.get_run(run["id"])["status"] == "interrupted"
    approvals.decide(rows[1]["id"], expected_fingerprint=rows[1]["fingerprint"], approve=False)
    resume_graph(context, graph, config)
    assert (context.workspace_root / "work/one.txt").read_text() == "one"
    assert not (context.workspace_root / "work/two.txt").exists()
    assert context.jobs.get_run(run["id"])["tool_calls"] == 1
    assert not context.gateway.client.reviews


@pytest.mark.parametrize("operation", ["write_file", "edit_file", "delete"])
def test_model_reviews_exact_file_target_and_execution_preserves_read_gate(runtime_services, tmp_path, operation):
    args = {"file_path": "/work/note.txt"}
    if operation == "write_file":
        args["content"] = "new"
    elif operation == "edit_file":
        args.update(old_string="old", new_string="new")
    batches = [] if operation == "write_file" else [[("read_file", {"file_path": "/work/note.txt"})]]
    batches.append([(operation, args)])
    module, context, _, graph, config = case(runtime_services, tmp_path, batches, mode="auto_review")
    path = context.workspace_root / "work/note.txt"
    if operation != "write_file":
        path.write_text("old")
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    assert not graph.get_state(config).interrupts
    if operation == "delete":
        assert not path.exists()
    else:
        assert path.read_text() == "new"
    assert len(context.gateway.client.reviews) == 1
    target = context.gateway.client.reviews[0]["facts"]["target"]
    assert target["path"] == "/work/note.txt"
    assert target["kind"] == ("missing" if operation == "write_file" else "file")
    if operation != "write_file":
        assert target["sha256"] and target["text"] == "old"


@pytest.mark.parametrize("path", ["/artifacts/forbidden.txt", "/skills/custom.py", "/knowledge/versions/a.md", "/"])
def test_managed_paths_never_reach_reviewer_or_human_approval(runtime_services, tmp_path, path):
    module, context, _, graph, config = case(runtime_services, tmp_path, [[
        ("write_file", {"file_path": path, "content": "forbidden"}),
    ]], mode="auto_review")
    result = graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    assert not graph.get_state(config).interrupts
    assert not context.gateway.client.reviews
    assert not ExecutionApprovals(context).list()
    assert any(isinstance(m, ToolMessage) and m.status == "error" for m in result["messages"])


def test_file_created_while_approval_is_open_cannot_receive_stale_human_grant(runtime_services, tmp_path):
    module, context, run, graph, config = case(runtime_services, tmp_path, [[
        ("write_file", {"file_path": "/work/note.txt", "content": "model"}),
    ]])
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    approvals, _ = save_wait(context, run, graph, config)
    row = approvals.list()[0]
    path = context.workspace_root / "work/note.txt"
    path.write_text("human")
    with pytest.raises(PermissionError, match="文件.*变化"):
        approvals.decide(row["id"], expected_fingerprint=row["fingerprint"], approve=True)
    assert path.read_text() == "human"
    assert context.jobs.get_run(run["id"])["status"] == "interrupted"
    approvals.decide(row["id"], expected_fingerprint=row["fingerprint"], approve=False)
    resume_graph(context, graph, config)
    assert path.read_text() == "human"


def test_file_changed_during_model_review_stops_stale_action_without_another_request(runtime_services, tmp_path):
    module, context, _, graph, config = case(runtime_services, tmp_path, [[
        ("write_file", {"file_path": "/work/note.txt", "content": "model"}),
    ]], mode="auto_review", during_review=lambda: (tmp_path / "workspace/work/note.txt").write_text("human"))
    result = graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    assert (context.workspace_root / "work/note.txt").read_text() == "human"
    assert len(context.gateway.client.reviews) == 1
    assert not graph.get_state(config).interrupts
    assert ExecutionApprovals(context).list()[0]["status"] == "invalidated"
    assert any(isinstance(m, ToolMessage) and "文件" in str(m.content) and m.status == "error"
               for m in result["messages"])


def test_auto_review_fallback_can_be_approved_once_but_not_saved_as_a_file_rule(runtime_services, tmp_path):
    module, context, run, graph, config = case(runtime_services, tmp_path, [[
        ("write_file", {"file_path": "/work/note.txt", "content": "allowed"}),
    ]], mode="auto_review", verdict="ask")
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    approvals, _ = save_wait(context, run, graph, config)
    row = approvals.list()[0]
    with pytest.raises(ValueError, match="逐次批准"):
        approvals.decide(row["id"], expected_fingerprint=row["fingerprint"], approve=True, scope="session")
    approvals.decide(row["id"], expected_fingerprint=row["fingerprint"], approve=True)
    resume_graph(context, graph, config)
    assert (context.workspace_root / "work/note.txt").read_text() == "allowed"
    assert len(context.gateway.client.reviews) == 1


@pytest.mark.parametrize("hazard", ["symlink", "fifo", "directory_fifo"])
def test_special_files_are_rejected_without_review_or_blocking(runtime_services, tmp_path, hazard):
    target = "/work/folder" if hazard == "directory_fifo" else "/work/hazard"
    module, context, _, graph, config = case(runtime_services, tmp_path, [[
        ("delete", {"file_path": target}),
    ]], mode="auto_review")
    path = context.workspace_root / target.lstrip("/")
    if hazard == "symlink":
        normal = context.workspace_root / "work/normal.txt"
        normal.write_text("keep")
        path.symlink_to(normal)
    elif hazard == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
        os.mkfifo(path / "pipe")
    result = graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    assert not graph.get_state(config).interrupts
    assert not context.gateway.client.reviews
    assert not ExecutionApprovals(context).list()
    assert path.exists()
    assert any(isinstance(m, ToolMessage) and m.status == "error" for m in result["messages"])


def test_same_content_replacement_after_human_approval_invalidates_original_file_identity(runtime_services, tmp_path):
    module, context, run, graph, config = case(runtime_services, tmp_path, [
        [("read_file", {"file_path": "/work/note.txt"})],
        [("edit_file", {"file_path": "/work/note.txt", "old_string": "old", "new_string": "new"})],
    ])
    path = context.workspace_root / "work/note.txt"
    path.write_text("old")
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    approvals, _ = save_wait(context, run, graph, config)
    row = approvals.list()[0]
    approvals.decide(row["id"], expected_fingerprint=row["fingerprint"], approve=True)
    replacement = path.with_suffix(".replacement")
    replacement.write_text("old")
    replacement.replace(path)
    resume_graph(context, graph, config)
    assert path.read_text() == "old"
    assert approvals.list()[0]["status"] == "invalidated"
    with context.jobs.sessions() as session:
        assert session.scalar(select(FileOperation)) is None


def test_final_file_boundary_rejects_change_after_tool_gate_and_closes_unexecuted_receipt(
    runtime_services, tmp_path, monkeypatch,
):
    from knowledgeops_agent.persistence.files import FileStore

    module, context, _, graph, config = case(runtime_services, tmp_path, [[
        ("write_file", {"file_path": "/work/note.txt", "content": "model"}),
    ]], mode="auto_review")
    path = context.workspace_root / "work/note.txt"
    original = FileStore.put

    def race(store, key, text, **kwargs):
        digest = original(store, key, text, **kwargs)
        if key.startswith("artifacts/file-operations/"):
            path.write_text("external")
        return digest

    monkeypatch.setattr(FileStore, "put", race)
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    assert path.read_text() == "external"
    assert len(context.gateway.client.reviews) == 1
    assert ExecutionApprovals(context).list()[0]["status"] == "invalidated"
    with context.jobs.sessions() as session:
        receipt = session.scalar(select(FileOperation))
        assert receipt.status == "skipped" and receipt.result["not_executed"]
        assert receipt.result["message"]["status"] == "error"


def test_file_mode_change_after_preparation_waits_and_resumes_the_original_operation(runtime_services, tmp_path, monkeypatch):
    import knowledgeops_agent.harness.sandbox.files as files
    from knowledgeops_agent.harness.permissions.approvals import ApprovalWaiting

    module, context, run, graph, config = case(runtime_services, tmp_path, [[
        ("write_file", {"file_path": "/work/note.txt", "content": "confirmed"}),
    ]], mode="auto_review")
    original = files.file_hash
    changed = False

    def prepared(path):
        nonlocal changed
        value = original(path)
        if not changed:
            with context.jobs.sessions() as session:
                exists = session.scalar(select(FileOperation.id).where(FileOperation.run_id == run["id"], FileOperation.status == "prepared"))
            if exists:
                changed = True
                ExecutionSettings(context.jobs.sessions).update(run["conversation_id"], expected_revision=1, permission_mode="ask")
        return value

    monkeypatch.setattr(files, "file_hash", prepared)
    with pytest.raises(ApprovalWaiting):
        graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    assert changed and not (context.workspace_root / "work/note.txt").exists()
    assert len(context.gateway.client.reviews) == 1
    with context.jobs.sessions() as session:
        operation = session.scalar(select(FileOperation).where(FileOperation.run_id == run["id"]))
        assert operation.status == "prepared"
        identity = operation.id
    context.jobs.finish(run["id"], context.worker_id, status="interrupted", outcome={"kind": "waiting_approval"})
    approvals = ExecutionApprovals(context)
    action = approvals.list()[0]
    assert action["status"] == "pending"
    approvals.decide(action["id"], expected_fingerprint=action["fingerprint"], approve=True)
    resume_graph(context, graph, config)
    assert (context.workspace_root / "work/note.txt").read_text() == "confirmed"
    assert len(context.gateway.client.reviews) == 1
    with context.jobs.sessions() as session:
        assert session.get(FileOperation, identity).status == "succeeded"


@pytest.mark.parametrize("pause_first", [False, True])
def test_cancelling_prepared_file_closes_unexecuted_receipt_even_without_a_worker(
    runtime_services, tmp_path, monkeypatch, pause_first,
):
    import knowledgeops_agent.harness.sandbox.files as files
    from knowledgeops_agent.app.lifecycle.service import LifecycleService
    from knowledgeops_agent.app.runs.service import RunPaused, RunStopped

    module, context, run, graph, config = case(runtime_services, tmp_path, [[
        ("write_file", {"file_path": "/work/cancelled.txt", "content": "must not write"}),
    ]], mode="auto_review")
    original, changed = files.file_hash, False

    def prepared(path):
        nonlocal changed
        value = original(path)
        if not changed:
            with context.jobs.sessions() as session:
                exists = session.scalar(select(FileOperation.id).where(
                    FileOperation.run_id == run["id"], FileOperation.status == "prepared"))
            if exists:
                changed = True
                (context.jobs.pause if pause_first else context.jobs.cancel)(run["id"])
        return value

    monkeypatch.setattr(files, "file_hash", prepared)
    with pytest.raises(RunPaused if pause_first else RunStopped):
        graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    assert changed and not (context.workspace_root / "work/cancelled.txt").exists()
    if pause_first:
        context.jobs.settle_pause(run["id"], context.worker_id, outcome={"kind": "paused"})
        with context.jobs.sessions() as session:
            assert session.scalar(select(FileOperation)).status == "prepared"
        # 没有正在执行的worker时，取消入口本身也必须结束准备回执。
        context.jobs.cancel(run["id"])
    else:
        context.jobs.finish(run["id"], context.worker_id)
    with context.jobs.sessions() as session:
        receipt = session.scalar(select(FileOperation))
        assert receipt.status == "skipped" and receipt.result["not_executed"]
        assert receipt.result["message"]["status"] == "error"
    assert len(context.gateway.client.reviews) == 1
    preview = LifecycleService(context.jobs.sessions, context.gateway.ledger).preview_root(run["id"], ignore_age=True)
    assert preview["eligible"], preview["blockers"]


def test_new_guidance_invalidates_prepared_file_without_applying_old_content(runtime_services, tmp_path, monkeypatch):
    from knowledgeops_agent.harness.sandbox.files import FileJournal

    module, context, run, graph, config = case(runtime_services, tmp_path, [[
        ("write_file", {"file_path": "/work/old-plan.txt", "content": "old plan"}),
    ]], mode="auto_review")
    original = FileJournal._start_prepared

    def guide_before_start(journal, row, scope, path):
        queued = context.jobs.enqueue(run["conversation_id"], "不要写入，先解释方案", request_key="withdraw-file")
        context.jobs.guide(queued["id"], target_id=run["id"], target_generation=run["generation"])
        return original(journal, row, scope, path)

    monkeypatch.setattr(FileJournal, "_start_prepared", guide_before_start)
    state = graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    # 引导信号由原RuntimeControl转为工具反馈，让模型先处理新指令。
    messages = [message for message in state["messages"] if isinstance(message, ToolMessage) and message.name == "write_file"]
    assert messages and messages[-1].status == "error"
    assert not (context.workspace_root / "work/old-plan.txt").exists()
    with context.jobs.sessions() as session:
        receipt = session.scalar(select(FileOperation))
        assert receipt.status == "skipped" and "用户引导" in receipt.result["not_executed"]
    assert ExecutionApprovals(context).list()[0]["status"] == "invalidated"


def test_file_worker_and_http_decision_resume_without_an_in_memory_tool_catalog(client, services, tmp_path):
    from uuid import uuid4

    from langgraph.checkpoint.memory import InMemorySaver
    from test_runtime import DraftProvider
    from test_workspace_runtime import enable

    from knowledgeops_agent.app.runs.worker import execute_run

    class Provider(ReviewedFiles):
        def invoke(self, messages, **kwargs):
            if "policy_version" not in json.loads(messages[-1].content):
                return DraftProvider("unused").invoke(messages, **kwargs)
            return super().invoke(messages, **kwargs)

    services.gateway.enabled = True
    services.gateway.client = Provider([[("write_file", {"file_path": "/work/from-http.txt", "content": "approved"})]])
    services.settings.workspace_root = tmp_path / "workspace"
    enable(services.sessions)
    conversation = client.post("/api/conversations", json={}).json()["id"]
    run = client.post(f"/api/conversations/{conversation}/messages", json={
        "content": "生成一个工作文件", "request_key": str(uuid4()),
    }).json()
    saver = InMemorySaver()
    result = execute_run(run["id"], 1, services=services, worker_id="file-api", checkpointer=saver)
    assert result["outcome"]["kind"] == "waiting_approval" and result["tool_calls"] == 0
    rows = client.get(f"/api/runs/{run['id']}/approvals").json()["actions"]
    assert len(rows) == 1 and rows[0]["rule_candidate"] is None
    row = rows[0]
    reply = client.post(f"/api/runs/{run['id']}/approvals/{row['id']}/decision", json={
        "expected_fingerprint": row["fingerprint"], "approve": True,
    })
    assert reply.status_code == 200, reply.text
    result = execute_run(run["id"], 2, services=services, worker_id="file-api", checkpointer=saver)
    assert result["status"] == "completed"
    assert (services.settings.workspace_root / "work/from-http.txt").read_text() == "approved"


def test_tool_revoked_during_review_closes_the_review_without_writing(runtime_services, tmp_path):
    from knowledgeops_agent.harness.tools.policy import ToolPolicyService

    module, context, _, graph, config = case(runtime_services, tmp_path, [[
        ("write_file", {"file_path": "/work/revoked.txt", "content": "forbidden"}),
    ]], mode="auto_review")

    def revoke():
        service = ToolPolicyService(context.jobs.sessions)
        service.update(expected_revision=service.get()["revision"], enabled={"middleware:write_file": False})

    context.gateway.client.during_review = revoke
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    assert not (context.workspace_root / "work/revoked.txt").exists()
    assert not graph.get_state(config).interrupts
    assert len(context.gateway.client.reviews) == 1
    row = ExecutionApprovals(context).list()[0]
    assert row["status"] == "denied"
    assert row["decision"]["review"]["state"] == "complete"
