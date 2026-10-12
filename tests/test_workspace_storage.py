"""真实 PG + 文件存储验证：正文迁移后引用、版本和检索仍指向同一资料。"""

import io
import json
import zipfile

import pytest
from sqlalchemy import select, text
from test_knowledge_service import publish_article

from knowledgeops_agent.db import KnowledgeVersion, RunArtifact
from knowledgeops_agent.file_store import FileStore
from knowledgeops_agent.jobs import RunService
from knowledgeops_agent.knowledge import KnowledgeService
from knowledgeops_agent.markdown_export import export_markdown
from knowledgeops_agent.sources import SourceService
from knowledgeops_agent.workspace_storage import migrate_content, sync_knowledge_projection


def test_published_body_is_file_backed_and_indexes_exports_still_work(business_sessions, tmp_path):
    store = FileStore(tmp_path)
    business_sessions.configure(info={"file_store": store})
    sources = SourceService(business_sessions, tmp_path / "uploads/originals")
    knowledge = KnowledgeService(business_sessions)
    _, _, article = publish_article(knowledge, sources)
    with business_sessions() as session:
        row = session.get(KnowledgeVersion, article["version_id"])
        assert row._body == ""
        assert row.body == article["body"]
        assert store.path(row.body_storage_key).read_text() == article["body"]
    assert knowledge.search("并发修改")
    with zipfile.ZipFile(io.BytesIO(export_markdown(business_sessions))) as archive:
        body = archive.read(f"documents/{article['document_id']}.md").decode()
        assert "../sources/" in body
    manifest = json.loads(store.path("knowledge/current/manifest.json").read_text())
    assert manifest["documents"][0]["version_id"] == article["version_id"]
    for path in manifest["managed_files"]:
        assert store.path("knowledge/current/" + path).is_file()


def test_nonempty_legacy_migration_preserves_ids_and_is_repeatable(business_sessions, tmp_path):
    old = tmp_path / "old"
    sources = SourceService(business_sessions, old)
    knowledge = KnowledgeService(business_sessions)
    _, evidence, article = publish_article(knowledge, sources)
    jobs = RunService(business_sessions)
    conversation = jobs.create_conversation()
    run = jobs.enqueue(conversation["id"], "测试", request_key="files")
    artifact_id = jobs.save_artifact(run["id"], "tool_result", "完整输出" * 100)
    expected = knowledge.search("并发")
    store = FileStore(tmp_path / "workspace")
    business_sessions.configure(info={"file_store": store})
    counts = migrate_content(business_sessions, legacy_source_root=old)
    assert counts["knowledge_versions"] == counts["run_artifacts"] == 1
    assert migrate_content(business_sessions, legacy_source_root=old)["knowledge_versions"] == 0
    assert knowledge.get_document(article["id"])["body"] == article["body"]
    assert knowledge.search("并发") == expected
    assert sources.get_evidence(evidence["id"])["id"] == evidence["id"]
    assert jobs.read_artifact(artifact_id, root_id=run["id"])["text"] == "完整输出" * 100
    assert jobs.save_artifact(run["id"], "tool_result", "完整输出" * 100) == artifact_id
    with business_sessions() as session:
        assert session.scalar(select(RunArtifact))._content == ""
        assert session.scalar(text("SELECT body FROM knowledge_versions")) == ""
    assert list(old.rglob("*")), "迁移不能删除旧原件"


def test_missing_or_changed_body_is_not_silently_replaced_by_database(business_sessions, tmp_path):
    business_sessions.configure(info={"file_store": FileStore(tmp_path)})
    knowledge = KnowledgeService(business_sessions)
    _, _, article = publish_article(
        knowledge, SourceService(business_sessions, tmp_path / "uploads/originals")
    )
    path = tmp_path / "knowledge/versions" / f"{article['version_id']}.md"
    path.write_text("外部错误改动")
    with pytest.raises(ValueError, match="integrity"):
        knowledge.get_document(article["id"])
    path.unlink()
    with pytest.raises(FileNotFoundError):
        sync_knowledge_projection(business_sessions)


def test_file_store_rejects_overwrite_and_escape(tmp_path):
    store = FileStore(tmp_path)
    digest = store.put("knowledge/versions/a.md", "正文")
    assert store.put("knowledge/versions/a.md", "正文") == digest
    with pytest.raises(ValueError, match="immutable"):
        store.put("knowledge/versions/a.md", "其他正文")
    with pytest.raises(ValueError):
        store.put("../outside", "x")
