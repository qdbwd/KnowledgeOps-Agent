"""隔离真实PG/保存器/文件验证：删除会话释放临时资产，保留独立和共享依赖。"""

import pytest
from sqlalchemy import select
from test_lifecycle_conversations import (
    checkpoint_case as checkpoint_case,
)
from test_lifecycle_conversations import (
    conversation_case as conversation_case,
)
from test_lifecycle_conversations import (
    detail_case as detail_case,
)
from test_lifecycle_conversations import (
    drain_conversation,
)
from test_lifecycle_conversations import (
    ledger as ledger,
)
from test_lifecycle_conversations import (
    redis_case as redis_case,
)

from knowledgeops_agent.app.lifecycle import assets
from knowledgeops_agent.persistence import models as db
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.sources.service import SourceService


def ready(cleaner):
    for _ in range(100):
        if cleaner.retained.catalog.step(limit=100)["complete"]:
            return
    pytest.fail("reference catalog did not finish")


def sources_for(cleaner, store):
    source_store = FileStore(store.root / "uploads")
    cleaner.sessions.configure(info={**cleaner.sessions.kw["info"], "source_store": source_store})
    return SourceService(cleaner.sessions, source_store.root)


def test_delete_temporary_assets_preserves_independent_and_other_chat_citations(conversation_case):
    cleaner, jobs, run, chat, store = conversation_case
    sources = sources_for(cleaner, store)
    private = sources.import_text("临时附件", "私人临时正文", conversation_id=chat["id"])
    cited = sources.import_text("被引用附件", "不能破坏的引用正文", conversation_id=chat["id"])
    saved = sources.import_text("主动保留", "主动保留正文", conversation_id=chat["id"])
    sources.save_source(saved["id"])
    unused_evidence = sources.read_segment(private["segments"][0]["id"], root_id=run["id"])
    evidence = sources.read_segment(cited["segments"][0]["id"], root_id=run["id"])
    other = jobs.create_conversation("另一个讨论")
    with cleaner.sessions.begin() as session:
        session.add(db.ChatMessage(conversation_id=other["id"], run_id=run["id"], role="assistant", content=evidence["citation"]))
        temporary = db.ChangeSet(root_id=run["id"], operations=[], retention_intent="temporary")
        independent = db.ChangeSet(root_id=run["id"], operations=[], retention_intent="saved")
        session.add_all([temporary, independent])
        session.flush()
        temporary_id, independent_id = temporary.id, independent.id
    ready(cleaner)
    preview = cleaner.preview(chat["id"])
    assert preview["assets"] == {"versions_delete": 1, "versions_keep": 2, "drafts_delete": 1, "drafts_keep": 1, "ready": True}
    assert preview["eligible"]
    with cleaner.sessions() as session:
        private_key = session.get(db.SourceVersion, private["version_id"]).storage_key
    result = drain_conversation(cleaner, cleaner.admit(chat["id"], expected_version=preview["version"])["id"])
    assert result["status"] == "completed", result
    assert result["summary"]["versions_deleted"] == 1 and result["summary"]["drafts_deleted"] == 1
    with cleaner.sessions() as session:
        assert session.get(db.Source, private["id"]) is None
        assert session.get(db.Evidence, unused_evidence["evidence_id"]) is None
        assert session.get(db.ChangeSet, temporary_id) is None
        assert session.get(db.ChangeSet, independent_id) is not None
        assert not list(session.scalars(select(db.SourceUse).where(db.SourceUse.owner_id == chat["id"])))
    assert not (sources.storage_root / private_key).exists()
    assert sources.get_version(cited["version_id"])["body_hash"] == cited["body_hash"]
    assert sources.get_source(saved["id"])["id"] == saved["id"]


def test_shared_blob_and_changed_preview_are_protected(conversation_case):
    cleaner, jobs, _, chat, store = conversation_case
    sources = sources_for(cleaner, store)
    private = sources.import_text("第一份", "相同物理原件", origin="a", conversation_id=chat["id"])
    kept = sources.import_text("第二份", "相同物理原件", origin="b")
    ready(cleaner)
    before = cleaner.preview(chat["id"])
    # 预览后保存意图变化，不能用旧预览直接删除。
    sources.save_source(private["id"])
    with pytest.raises(ValueError, match="状态已变化"):
        cleaner.admit(chat["id"], expected_version=before["version"])
    with cleaner.sessions.begin() as session:
        session.delete(session.get(db.SourceUse, (private["id"], "library", "default")))
    preview = cleaner.preview(chat["id"])
    result = drain_conversation(cleaner, cleaner.admit(chat["id"], expected_version=preview["version"])["id"])
    assert result["status"] == "completed", result
    assert sources.original(kept["version_id"])[0].decode() == "相同物理原件"


def test_source_file_cleanup_retries_after_unlink_without_reopening_removed_metadata(conversation_case, monkeypatch):
    cleaner, _, _, chat, store = conversation_case
    sources = sources_for(cleaner, store)
    private = sources.import_text("重试附件", "只应删除一次", conversation_id=chat["id"])
    ready(cleaner)
    preview = cleaner.preview(chat["id"])
    job = cleaner.admit(chat["id"], expected_version=preview["version"])
    original = assets._unlink

    def fail_after_unlink(*args):
        original(*args)
        raise OSError("simulated crash")

    monkeypatch.setattr(assets, "_unlink", fail_after_unlink)
    failed = drain_conversation(cleaner, job["id"])
    assert failed["status"] == "failed" and failed["phase"] == "conversation_asset_files", failed
    with cleaner.sessions() as session:
        assert session.get(db.SourceVersion, private["version_id"]) is None
    # 重试窗口中重新导入相同内容是新身份；旧文件作业不能删除它重新使用的原件。
    replacement = sources.import_text("重新保存", "只应删除一次")
    monkeypatch.setattr(assets, "_unlink", original)
    cleaner.retry(job["id"])
    assert drain_conversation(cleaner, job["id"])["status"] == "completed"
    assert sources.original(replacement["version_id"])[0].decode() == "只应删除一次"


def test_fixed_citation_keeps_only_used_version_and_later_versions_remain_monotonic(conversation_case):
    cleaner, jobs, run, chat, store = conversation_case
    sources = sources_for(cleaner, store)
    first = sources.import_text("多版资料", "没有引用的旧版", origin="fixture:versions", conversation_id=chat["id"])
    second = sources.import_text("多版资料", "有引用的新版", origin="fixture:versions", conversation_id=chat["id"])
    evidence = sources.read_segment(second["segments"][0]["id"], root_id=run["id"])
    other = jobs.create_conversation("保留新版引用")
    with cleaner.sessions.begin() as session:
        session.add(db.ChatMessage(conversation_id=other["id"], run_id=run["id"], role="assistant", content=evidence["citation"]))
    ready(cleaner)
    preview = cleaner.preview(chat["id"])
    assert preview["assets"]["versions_delete"] == 1 and preview["assets"]["versions_keep"] == 1
    result = drain_conversation(cleaner, cleaner.admit(chat["id"], expected_version=preview["version"])["id"])
    assert result["status"] == "completed", result
    with pytest.raises(KeyError):
        sources.get_version(first["version_id"])
    assert sources.get_version(second["version_id"])["version"] == 2
    third = sources.import_text("多版资料", "第三版内容", origin="fixture:versions")
    assert third["version"] == 3
