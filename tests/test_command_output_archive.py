"""短命令输出流先落盘，预览截断不会丢掉保存上限内的尾部。"""

import shlex
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from knowledgeops_agent.app.runs.service import RunService, RunStopped
from knowledgeops_agent.harness.sandbox.execution import execute_local_command
from knowledgeops_agent.harness.sandbox.output import CommandOutput, output_scope
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.persistence.models import RunArtifact


@pytest.fixture
def context(business_sessions, tmp_path):
    store = FileStore(tmp_path)
    business_sessions.configure(info={"file_store": store})
    jobs = RunService(business_sessions)
    conversation = jobs.create_conversation()
    run = jobs.enqueue(conversation["id"], "保存输出", request_key="output")
    jobs.claim("output-worker", run_id=run["id"], generation=1)
    return SimpleNamespace(jobs=jobs, run_id=run["id"], root_id=run["id"], resources={},
                           gateway=SimpleNamespace(sensitive_values=["a-private-test-token"]),
                           workspace_root=tmp_path)


def run_command(context, command, *, limit=1048576, timeout=3, guard=lambda: None):
    with output_scope(context, limit_bytes=limit) as output:
        result = execute_local_command(command, cwd=context.workspace_root,
                                       env={"PATH": "/workspace/.venv/bin:/usr/bin:/bin"},
                                       timeout=timeout, guard=guard)
    return result, output


def test_large_output_is_file_backed_and_tail_is_readable_after_preview(context):
    script = "print('A' * 90000 + 'TAIL')"
    result, output = run_command(context, "python -c " + shlex.quote(script))
    assert result.exit_code == 0 and result.truncated
    assert "TAIL" not in result.output and "read_tool_result" in result.output
    assert len(result.output) < 6000
    tail = context.jobs.read_artifact(output.metadata["artifact_id"], root_id=context.root_id, start=90000)
    assert tail["text"] == "TAIL\n"
    assert not output.metadata["storage_truncated"]
    with context.jobs.sessions() as session:
        row = session.scalar(select(RunArtifact).where(RunArtifact.id == output.metadata["artifact_id"]))
        assert row._content == "" and row.content_storage_key


def test_output_limit_still_drains_and_records_truncation(context):
    result, output = run_command(context, "python -c " + shlex.quote("print('文' * 40000)"), limit=1024)
    assert result.exit_code == 0 and result.truncated
    assert output.metadata["storage_truncated"] and output.metadata["saved_bytes"] <= 1024
    assert output.metadata["received_bytes"] > 100000
    text = context.jobs.artifact_content(output.metadata["artifact_id"], root_id=context.root_id)
    assert "\ufffd" not in text


def test_split_secret_is_redacted_before_stream_is_persisted(context):
    with output_scope(context, limit_bytes=1024) as output:
        output.append(b"prefix a-private-")
        output.append(b"test-token suffix")
        output.finish(exit_code=0)
    text = context.jobs.artifact_content(output.metadata["artifact_id"], root_id=context.root_id)
    assert "a-private-test-token" not in text and "[REDACTED]" in text
    assert CommandOutput.DEFAULT_LIMIT == 10 * 1024 * 1024


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancellation_preserve_received_output(context, cancel):
    def guard():
        if cancel and (context.workspace_root / "started").exists():
            raise RunStopped("cancelled")
    with output_scope(context, limit_bytes=1024) as output:
        if cancel:
            with pytest.raises(RunStopped):
                execute_local_command("printf partial; touch started; sleep 10",
                                      cwd=context.workspace_root, env={"PATH": "/usr/bin:/bin"},
                                      timeout=3, guard=guard)
        else:
            result = execute_local_command("printf partial; sleep 10", cwd=context.workspace_root,
                                           env={"PATH": "/usr/bin:/bin"}, timeout=1, guard=guard)
            assert result.exit_code == 124
    assert context.jobs.artifact_content(output.metadata["artifact_id"], root_id=context.root_id) == "partial"
    assert output.metadata["stop_reason"] == ("RunStopped" if cancel else "timeout")


def test_real_graph_execute_receipt_contains_streamed_artifact(business_sessions, tmp_path):
    import json
    from uuid import uuid4

    from langchain_core.messages import AIMessageChunk, ToolMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from test_runtime import setup_run
    from test_workspace_runtime import enable

    from knowledgeops_agent.harness.models.gateway import ModelLedger, prepare_accounting
    from knowledgeops_agent.harness.permissions.receipts import CommandJournal
    from knowledgeops_agent.harness.permissions.settings import ExecutionSettings
    from knowledgeops_agent.knowledge.service import KnowledgeService
    from knowledgeops_agent.sources.service import SourceService

    business_sessions.configure(info={"file_store": FileStore(tmp_path / "workspace")})
    engine = business_sessions.kw["bind"]
    prepare_accounting(engine)
    jobs = RunService(business_sessions)
    services = (jobs, KnowledgeService(business_sessions), SourceService(business_sessions, tmp_path / "sources"),
                ModelLedger(engine, scope="output-" + uuid4().hex))
    module, context, run = setup_run(services)
    context.workspace_root = tmp_path / "workspace"
    enable(business_sessions)
    ExecutionSettings(business_sessions).update(run["conversation_id"], expected_revision=0, permission_mode="auto")
    class Provider:
        def bind_tools(self, tools):
            return self
        def stream(self, messages, **kwargs):
            if any(isinstance(item, ToolMessage) for item in messages):
                yield AIMessageChunk("命令已执行。")
            else:
                yield AIMessageChunk("", tool_call_chunks=[{
                    "name": "execute", "args": json.dumps({"command": "printf 'stored-result'"}),
                    "id": "record-output", "index": 0,
                }])
    context.gateway.client = Provider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    state = graph.invoke({"messages": module.initial_messages(context)},
                         {"configurable": {"thread_id": run["checkpoint_id"]}}, context=context)
    result = next(item for item in state["messages"] if isinstance(item, ToolMessage))
    metadata = result.artifact["command_output"]
    assert context.jobs.artifact_content(metadata["artifact_id"], root_id=run["id"]) == "stored-result"
    receipt = CommandJournal(context).list()[0]
    assert receipt["status"] == "succeeded"
    assert receipt["result"]["artifact"]["command_output"] == metadata
