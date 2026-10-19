"""通用文件工具也必须完成知识版本闭环；真实 SDK/图/PG，仅模型响应离线。"""

import json

from fixtures.execution_provider import ExecutionProvider
from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run
from test_workspace_runtime import enable

from knowledgeops_agent.harness.permissions.settings import ExecutionSettings
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.persistence.models import ChangeSet, KnowledgeDocument, KnowledgeVersion

runtime_services = _runtime_services


def run_files(services, root, steps, *, mode="direct"):
    module, context, run = setup_run(services, mode=mode)
    # 此组验证通用文件操作到业务版本的同步，命令执行许可显式独立设置。
    ExecutionSettings(context.jobs.sessions).update(
        run["conversation_id"], expected_revision=0, permission_mode="auto_review"
    )
    context.workspace_root = root
    context.jobs.sessions.configure(info={"file_store": FileStore(root)})
    enable(context.jobs.sessions)

    class Provider:
        invoke = ExecutionProvider.invoke

        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            seen = [message for message in messages if isinstance(message, ToolMessage)]
            if len(seen) == len(steps):
                yield AIMessageChunk("文件操作结束，请查看对应回执。")
                return
            name, args = steps[len(seen)]
            yield AIMessageChunk(
                "",
                tool_call_chunks=[
                    {
                        "name": name,
                        "args": json.dumps(args),
                        "id": f"native-{len(seen)}",
                        "index": 0,
                    }
                ],
            )

    context.gateway.client = Provider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    result = graph.invoke(
        {"messages": module.initial_messages(context)},
        {"configurable": {"thread_id": run["checkpoint_id"]}},
        context=context,
    )
    return context, [message for message in result["messages"] if isinstance(message, ToolMessage)]


def test_native_write_edit_shell_and_delete_keep_versions_and_index(runtime_services, tmp_path):
    path = "/knowledge/documents/机制/工具.md"
    context, observations = run_files(
        runtime_services,
        tmp_path,
        [
            ("write_file", {"file_path": path, "content": "# 工具循环\n原始正文"}),
            ("read_file", {"file_path": path}),
            ("edit_file", {"file_path": path, "old_string": "原始正文", "new_string": "反馈执行"}),
            (
                "execute",
                {
                    "command": "printf '\n脚本追加' >> knowledge/documents/机制/工具.md",
                    "timeout": 5,
                },
            ),
            ("read_file", {"file_path": path}),
            ("delete", {"file_path": path}),
        ],
    )
    for message in observations:
        assert message.status != "error", message.content
        if message.name in {"write_file", "edit_file", "execute", "delete"}:
            assert '"status": "published"' in message.text, message.text
    with context.jobs.sessions() as session:
        doc = session.scalar(select(KnowledgeDocument))
        versions = list(session.scalars(select(KnowledgeVersion).order_by(KnowledgeVersion.number)))
        assert doc.workspace_path == path.lstrip("/")
        assert len(versions) == 4 and versions[-1].archived
        assert versions[0].body == "# 工具循环\n原始正文"
        assert "脚本追加" in versions[2].body
        assert all(not row._body for row in versions)
    assert not (tmp_path / path.lstrip("/")).exists()
    assert not context.knowledge.search("反馈执行")


def test_preview_repeated_edits_revise_one_draft_and_approval_keeps_path(
    runtime_services, tmp_path
):
    path = "/knowledge/documents/预览.md"
    context, observations = run_files(
        runtime_services,
        tmp_path,
        [
            ("write_file", {"file_path": path, "content": "# 预览\n待完善内容"}),
            ("read_file", {"file_path": path}),
            ("edit_file", {"file_path": path, "old_string": "待完善", "new_string": "已经修订"}),
        ],
        mode="preview",
    )
    assert '"status": "draft_ready"' in observations[-1].text
    drafts = context.knowledge.list_drafts(root_id=context.root_id)
    assert len(drafts) == 1 and drafts[0]["revision"] == 2
    assert context.knowledge.list_documents() == []
    receipt = context.knowledge.publish(
        drafts[0]["id"], idempotency_key="ui-confirm", user_approval=True, expected_revision=2
    )
    doc = context.knowledge.get_document(receipt["document_ids"][0])
    assert doc["body"] == "# 预览\n已经修订内容"
    assert (tmp_path / path.lstrip("/")).read_text() == doc["body"]
    assert context.knowledge.search("已经修订")
    # 重放旧提交不能覆盖用户在发布之后继续编辑但尚未同步的正文。
    (tmp_path / path.lstrip("/")).write_text("# 预览\n后续人工修改")
    replay = context.knowledge.publish(drafts[0]["id"], idempotency_key="ui-confirm")
    assert replay["workspace_export"]["pending_working_files"] == [path.lstrip("/")]
    assert "后续人工修改" in (tmp_path / path.lstrip("/")).read_text()
    (tmp_path / path.lstrip("/")).unlink()
    replay = context.knowledge.publish(drafts[0]["id"], idempotency_key="ui-confirm")
    assert replay["workspace_export"]["pending_working_files"] == [path.lstrip("/")]
    assert not (tmp_path / path.lstrip("/")).exists(), "旧发布重放不能复活刚删除的工作文件"


def test_deleting_unpublished_file_rejects_its_pending_draft(runtime_services, tmp_path):
    context, _ = run_files(
        runtime_services,
        tmp_path,
        [
            (
                "write_file",
                {"file_path": "/knowledge/documents/取消.md", "content": "# 取消\n不再保留"},
            ),
            ("read_file", {"file_path": "/knowledge/documents/取消.md"}),
            ("delete", {"file_path": "/knowledge/documents/取消.md"}),
        ],
        mode="preview",
    )
    with context.jobs.sessions() as session:
        assert session.scalar(select(ChangeSet)).status == "rejected"
        assert session.scalar(select(KnowledgeDocument)) is None


def test_invalid_citation_preserves_working_file_without_false_publication(
    runtime_services, tmp_path
):
    path = "/knowledge/documents/待修复.md"
    context, observations = run_files(
        runtime_services,
        tmp_path,
        [
            (
                "write_file",
                {
                    "file_path": path,
                    "content": "# 待修复\n[来源](/api/evidence/00000000-0000-0000-0000-000000000001)",
                },
            ),
        ],
    )
    assert '"status": "sync_failed"' in observations[0].text
    assert (tmp_path / path.lstrip("/")).exists()
    assert context.knowledge.list_documents() == []


def test_external_edit_can_be_explicitly_synchronized_without_duplicate_revision(
    runtime_services, tmp_path
):
    from langchain.tools import ToolRuntime

    from knowledgeops_agent.knowledge.files import sync_knowledge_files

    path = "/knowledge/documents/外部编辑.md"
    context, _ = run_files(
        runtime_services,
        tmp_path,
        [
            ("write_file", {"file_path": path, "content": "# 外部编辑\n初稿"}),
        ],
        mode="preview",
    )
    (tmp_path / path.lstrip("/")).write_text("# 外部编辑\nWindows用户修订")
    runtime = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="external-sync",
        store=None,
    )
    first = sync_knowledge_files.func([path], runtime)
    second = sync_knowledge_files.func([path], runtime)
    assert first["status"] == second["status"] == "draft_ready"
    assert first["changeset_id"] == second["changeset_id"]
    draft = context.knowledge.get_draft(first["changeset_id"])
    assert draft["revision"] == 2
    assert "Windows用户修订" in draft["operations"][0]["body"]
