"""真实 SDK 文件工具、Agent 图和 PG 回执；不调用真实模型。"""

import json

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run
from test_workspace_runtime import enable

from knowledgeops_agent.persistence.models import FileOperation

runtime_services = _runtime_services


class FileProvider:
    def __init__(self, batches, *, before_response=None):
        self.batches = batches
        self.position = 0
        self.before_response = before_response

    def bind_tools(self, tools):
        return self

    def stream(self, messages, **kwargs):
        index = self.position
        self.position += 1
        if self.before_response:
            self.before_response(index)
        if index >= len(self.batches):
            yield AIMessageChunk(content="完成核对。")
            return
        yield AIMessageChunk(content="", tool_call_chunks=[
            {"name": name, "args": json.dumps(args, ensure_ascii=False),
             "id": f"file-{index}-{position}", "index": position}
            for position, (name, args) in enumerate(self.batches[index])
        ])


def graph_case(runtime_services, tmp_path, batches, *, publication_mode="preview", **kwargs):
    module, context, run = setup_run(runtime_services, mode=publication_mode)
    context.workspace_root = tmp_path / "workspace"
    enable(context.jobs.sessions)
    context.gateway.client = FileProvider(batches, **kwargs)
    saver = InMemorySaver()
    graph = module.build_agent(context, checkpointer=saver)
    config = {"configurable": {"thread_id": run["checkpoint_id"]}}
    return module, context, graph, config


def test_external_edit_and_unread_edit_are_rejected(runtime_services, tmp_path):
    path = tmp_path / "workspace/work/note.txt"
    path.parent.mkdir(parents=True)
    path.write_text("old")
    def change(index):
        if index == 2:
            path.write_text("external")
    module, context, graph, config = graph_case(runtime_services, tmp_path, [
        [("edit_file", {"file_path": "/work/note.txt", "old_string": "old", "new_string": "unread"})],
        [("read_file", {"file_path": "/work/note.txt"})],
        [("edit_file", {"file_path": "/work/note.txt", "old_string": "external", "new_string": "stale"})],
    ], before_response=change)
    result = graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert [m.status for m in messages] == ["error", "success", "error"]
    assert "重新读取" in messages[-1].content
    assert path.read_text() == "external"


def test_parallel_writes_cannot_reuse_same_read_even_if_content_unchanged(runtime_services, tmp_path):
    path = tmp_path / "workspace/work/note.txt"
    path.parent.mkdir(parents=True)
    path.write_text("old")
    module, context, graph, config = graph_case(runtime_services, tmp_path, [
        [("read_file", {"file_path": "/work/note.txt"})],
        [("edit_file", {"file_path": "/work/note.txt", "old_string": "old", "new_string": "old"})] * 2,
    ])
    result = graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    edits = [m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "edit_file"]
    assert sorted(m.status for m in edits) == ["error", "success"]
    with context.jobs.sessions() as session:
        assert len(session.scalars(select(FileOperation)).all()) == 1


def test_crash_after_atomic_replace_recovers_without_rewriting(runtime_services, tmp_path, monkeypatch):
    from knowledgeops_agent.persistence.files import FileStore
    path = tmp_path / "workspace/work/note.txt"
    path.parent.mkdir(parents=True)
    path.write_text("old")
    module, context, graph, config = graph_case(runtime_services, tmp_path, [
        [("read_file", {"file_path": "/work/note.txt"})],
        [("edit_file", {"file_path": "/work/note.txt", "old_string": "old", "new_string": "new"})],
    ])
    original = FileStore.replace
    writes = []
    def crash_after_write(store, key, data):
        original(store, key, data)
        if key == "work/note.txt":
            writes.append(key)
            raise ConnectionError("simulated process loss after replace")
    monkeypatch.setattr(FileStore, "replace", crash_after_write)
    with pytest.raises(ConnectionError, match="process loss"):
        graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    assert path.read_text() == "new"
    result = graph.invoke(None, config, context=context)
    assert result["messages"][-1].content == "完成核对。"
    assert writes == ["work/note.txt"]
    with context.jobs.sessions() as session:
        row = session.scalar(select(FileOperation))
        assert row.status == "succeeded"
    assert context.jobs.get_run(context.run_id)["tool_calls"] == 2


@pytest.mark.parametrize("external_change", [False, True])
def test_uncertain_write_is_not_retried_or_allowed_to_overwrite_external_content(
    runtime_services, tmp_path, monkeypatch, external_change,
):
    from knowledgeops_agent.harness.permissions.receipts import ExecutionUnknown
    from knowledgeops_agent.persistence.files import FileStore

    path = tmp_path / "workspace/work/note.txt"
    path.parent.mkdir(parents=True)
    path.write_text("old")
    module, context, graph, config = graph_case(runtime_services, tmp_path, [
        [("read_file", {"file_path": "/work/note.txt"})],
        [("edit_file", {"file_path": "/work/note.txt", "old_string": "old", "new_string": "new"})],
    ])
    attempts = []
    def interrupted(store, key, data):
        attempts.append(key)
        raise ConnectionError("lost before filesystem confirmation")
    monkeypatch.setattr(FileStore, "replace", interrupted)
    with pytest.raises(ConnectionError):
        graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    if external_change:
        path.write_text("human version")
    with pytest.raises(ExecutionUnknown):
        graph.invoke(None, config, context=context)
    assert path.read_text() == ("human version" if external_change else "old")
    assert attempts == ["work/note.txt"]
    with context.jobs.sessions() as session:
        assert session.scalar(select(FileOperation)).status == ("conflict" if external_change else "unknown")


@pytest.mark.parametrize("limit,offset", [(0, 0), (2, 1000)])
def test_read_without_content_does_not_authorize_modification(runtime_services, tmp_path, limit, offset):
    path = tmp_path / "workspace/work/note.txt"
    path.parent.mkdir(parents=True)
    path.write_text("old")
    module, context, graph, config = graph_case(runtime_services, tmp_path, [
        [("read_file", {"file_path": "/work/note.txt", "offset": offset, "limit": limit})],
        [("edit_file", {"file_path": "/work/note.txt", "old_string": "old", "new_string": "new"})],
    ])
    result = graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    assert [m for m in result["messages"] if isinstance(m, ToolMessage)][-1].status == "error"
    assert path.read_text() == "old"


def test_compacted_read_is_not_a_permanent_permission(runtime_services, tmp_path):
    path = tmp_path / "workspace/work/note.txt"
    path.parent.mkdir(parents=True)
    path.write_text("old")
    module, context, graph, config = graph_case(runtime_services, tmp_path, [
        [("read_file", {"file_path": "/work/note.txt"})],
        [("edit_file", {"file_path": "/work/note.txt", "old_string": "old", "new_string": "new"})],
    ])
    result = graph.invoke({"messages": module.initial_messages(context)}, config, context=context, interrupt_after=["tools"])
    read = result["messages"][-1]
    graph.update_state(config, {"context_compaction": {
        "last_message_id": read.id, "summary": "已经检查过文件，原文已压缩。",
        "summary_artifact_id": "fixture-summary", "original_artifact_id": "fixture-original",
    }})
    result = graph.invoke(None, config, context=context)
    edits = [m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "edit_file"]
    assert edits[-1].status == "error" and path.read_text() == "old"


def test_directory_delete_checks_reads_and_preserves_receipt_copy(runtime_services, tmp_path):
    folder = tmp_path / "workspace/work/directory"
    folder.mkdir(parents=True)
    (folder / "file.txt").write_text("contents")
    module, context, graph, config = graph_case(runtime_services, tmp_path, [
        [("delete", {"file_path": "/work/directory"})],
        [("ls", {"path": "/work/directory"})],
        [("read_file", {"file_path": "/work/directory/file.txt"})],
        [("delete", {"file_path": "/work/directory"})],
    ])
    result = graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    deletes = [m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "delete"]
    assert [m.status for m in deletes] == ["error", "success"]
    assert not folder.exists()
    with context.jobs.sessions() as session:
        row = session.scalar(select(FileOperation))
        assert (context.workspace_root / "artifacts/file-operations" / row.id / "deleted/file.txt").read_text() == "contents"


@pytest.mark.parametrize("crash_at", ["before_sync", "after_sync"])
def test_knowledge_sync_recovers_without_duplicate_write_or_version(
    runtime_services, tmp_path, monkeypatch, crash_at,
):
    from knowledgeops_agent.harness.sandbox.files import FileJournal
    from knowledgeops_agent.knowledge import files
    from knowledgeops_agent.persistence.files import FileStore

    module, context, graph, config = graph_case(runtime_services, tmp_path, [
        [("write_file", {"file_path": "/knowledge/documents/article.md", "content": "# 标题\n正文"})],
    ], publication_mode="direct")
    context.jobs.sessions.configure(info={"file_store": FileStore(context.workspace_root)})
    original_sync, original_finish = files.synchronize, FileJournal.finish
    crashes = []
    def synchronize(*args, **kwargs):
        if crash_at == "before_sync" and not crashes:
            crashes.append("before_sync")
            raise RuntimeError("process stopped before synchronization")
        return original_sync(*args, **kwargs)
    def finish(journal, call, result):
        if crash_at == "after_sync" and not crashes:
            crashes.append("after_sync")
            raise RuntimeError("process stopped before graph receipt was saved")
        return original_finish(journal, call, result)
    monkeypatch.setattr(files, "synchronize", synchronize)
    monkeypatch.setattr(FileJournal, "finish", finish)
    with pytest.raises(RuntimeError):
        graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    path = context.workspace_root / "knowledge/documents/article.md"
    modified = path.stat().st_mtime_ns
    # 重建图和后端实例，只复用已持久化检查点/回执；不依赖上个实例中的文件状态。
    resumed = module.build_agent(context, checkpointer=graph.checkpointer)
    result = resumed.invoke(None, config, context=context)
    assert result["messages"][-1].content == "完成核对。"
    assert path.stat().st_mtime_ns == modified
    documents = context.knowledge.list_documents()
    assert len(documents) == 1 and len(context.knowledge.history(documents[0]["document_id"])) == 1
    with context.jobs.sessions() as session:
        assert session.scalar(select(FileOperation)).status == "succeeded"
