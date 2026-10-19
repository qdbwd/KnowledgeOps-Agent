"""历史HTML显式整理的合同；这里的恢复报告替身只验证门禁，真实恢复另有演练。"""

import json

import pytest
from sqlalchemy import select

from knowledgeops_agent.app.lifecycle.legacy_html import LegacyHtmlCleaner
from knowledgeops_agent.app.runs.service import RunService
from knowledgeops_agent.persistence import models as db
from knowledgeops_agent.persistence.backup import file_hash
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.sources.service import SourceService


@pytest.fixture
def html_case(business_sessions, tmp_path):
    store = FileStore(tmp_path / "uploads")
    business_sessions.configure(info={"source_store": store})
    sources = SourceService(business_sessions, store.root)
    cleaner = LegacyHtmlCleaner(business_sessions)
    return cleaner, sources, store


def old_web(case, *, body="旧正文仍完整。", html=b"<html>original HTML</html>", origin=None):
    cleaner, sources, _ = case
    item = sources.import_text("旧网页", body, kind="file", raw_bytes=html, mime_type="text/html",
                               origin=origin or db.new_id(), parser="trafilatura-v2")
    with cleaner.sessions.begin() as session:
        session.get(db.Source, item["id"]).kind = "web"
        version = session.get(db.SourceVersion, item["version_id"])
        version.body_hash, version.body_format, version.original_hash = None, None, None
    return item


def proof(tmp_path, cleaner, items):
    """仅用于确定性门禁测试；这些文件不是可以用于正式清理的真实备份。"""
    root = tmp_path / "proof"
    root.mkdir()
    for name in ("business.dump", "accounting.dump", "workspace.tar.gz"):
        (root / name).write_bytes(b"synthetic test fixture")
    manifest = {"format": 2, "source_database": cleaner.sessions.kw["bind"].url.database,
                "files": {name: file_hash(root / name) for name in ("business.dump", "accounting.dump", "workspace.tar.gz")}}
    (root / "manifest.json").write_text(json.dumps(manifest))
    report = {"source_database": manifest["source_database"], "backup_manifest_sha256": file_hash(root / "manifest.json"),
              "checks": {key: True for key in ("all_original_hashes", "all_saved_bodies_readable", "all_citations_resolve",
                         "all_delivered_files_readable", "all_learning_profile_revisions_readable", "index_rebuild_equivalent")},
              "bodies": [{"version_id": item["version_id"], "sha256": item["body_hash"]} for item in items],
              "files": [{"version_id": item["version_id"], "sha256": item["original_hash"], "matches": True} for item in items]}
    (root / "restore-verification.json").write_text(json.dumps(report))
    return root


def test_preview_is_read_only_and_cleanup_keeps_body_ids_and_citations(html_case, tmp_path):
    cleaner, sources, store = html_case
    item = old_web(html_case, body="第一段。" * 1000)
    runs = RunService(cleaner.sessions)
    chat = runs.create_conversation("保留引用")
    run = runs.enqueue(chat["id"], "读取", request_key=db.new_id())
    evidence = sources.read_segment(item["segments"][1]["id"], root_id=run["id"])
    with cleaner.sessions() as session:
        before = dict(session.execute(db.SourceVersion.__table__.select()).mappings().one())
    preview = cleaner.preview()
    entry = preview["items"][0]
    assert entry["eligible"] and entry["evidence_count"] == 1
    with cleaner.sessions() as session:
        assert dict(session.execute(db.SourceVersion.__table__.select()).mappings().one()) == before
        assert session.scalar(select(db.CleanupJob.id)) is None
    backup = proof(tmp_path, cleaner, [entry])
    job = cleaner.admit(entry, backup_folder=backup)
    assert cleaner.admit(entry, backup_folder=backup)["id"] == job["id"]
    assert store.path(entry["storage_key"]).exists()
    assert cleaner.step(identity=job["id"])["phase"] == "html_files"
    assert store.path(entry["storage_key"]).exists()  # 解除关联已经提交，物理删除留到下一步。
    assert cleaner.step(identity=job["id"])["status"] == "completed"
    assert not store.path(entry["storage_key"]).exists()
    assert sources.full_content(item["version_id"])["text"] == "第一段。" * 1000
    assert sources.get_evidence(evidence["id"]) == evidence
    with cleaner.sessions() as session:
        after = dict(session.execute(db.SourceVersion.__table__.select()).mappings().one())
        for key in before.keys() - {"storage_key", "body_hash", "body_format", "original_hash"}:
            assert before[key] == after[key]


def test_absent_changed_or_wrong_restore_cannot_admit(html_case, tmp_path):
    cleaner, _, _ = html_case
    old_web(html_case)
    entry = cleaner.preview()["items"][0]
    with pytest.raises(FileNotFoundError):
        cleaner.admit(entry, backup_folder=tmp_path)
    root = proof(tmp_path, cleaner, [entry])
    (root / "business.dump").write_bytes(b"changed after restore")
    with pytest.raises(ValueError, match="integrity"):
        cleaner.admit(entry, backup_folder=root)
    with cleaner.sessions() as session:
        assert session.scalar(select(db.CleanupJob.id)) is None


@pytest.mark.parametrize("change", ["database", "manifest", "body", "failed_check"])
def test_restore_proof_must_match_the_database_version_and_success(html_case, tmp_path, change):
    cleaner, _, _ = html_case
    old_web(html_case)
    entry = cleaner.preview()["items"][0]
    root = proof(tmp_path, cleaner, [entry])
    path = root / "restore-verification.json"
    report = json.loads(path.read_text())
    if change == "database":
        report["source_database"] = "unrelated"
    elif change == "manifest":
        report["backup_manifest_sha256"] = "0" * 64
    elif change == "body":
        report["bodies"][0]["sha256"] = "0" * 64
    else:
        report["checks"]["all_citations_resolve"] = False
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        cleaner.admit(entry, backup_folder=root)
    with cleaner.sessions() as session:
        assert session.scalar(select(db.CleanupJob.id)) is None


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_preview_rejects_non_regular_original_without_reading_it(html_case, kind):
    import os

    cleaner, _, store = html_case
    old_web(html_case)
    original = store.path(cleaner.preview()["items"][0]["storage_key"])
    original.unlink()
    if kind == "symlink":
        outside = store.root.parent / "unmanaged.html"
        outside.write_text("must remain untouched")
        original.symlink_to(outside)
    else:
        os.mkfifo(original)
    item = cleaner.preview()["items"][0]
    assert not item["eligible"] and item["reason"]
    assert original.lstat()


def test_backup_verifier_checks_citation_text_not_only_existence(html_case):
    from scripts.backup_fixture import verify_citations

    cleaner, _, _ = html_case
    item = old_web(html_case, body="第一句。第二句。")
    with cleaner.sessions.begin() as session:
        evidence = db.Evidence(root_id="old", source_segment_id=item["segments"][0]["id"],
                               start_offset=4, end_offset=8, quote="第二句。")
        session.add(evidence)
        session.flush()
        assert verify_citations(session) == [evidence.id]
        evidence.quote = "不同原文"
        with pytest.raises(ValueError, match="citation does not match"):
            verify_citations(session)


def test_body_gap_corruption_and_invalid_citation_are_reported(html_case):
    cleaner, _, store = html_case
    a = old_web(html_case, origin="gap")
    b = old_web(html_case, origin="citation", html=b"<html>second</html>")
    c = old_web(html_case, origin="file", html=b"<html>third</html>")
    with cleaner.sessions.begin() as session:
        session.get(db.SourceSegment, a["segments"][0]["id"]).end_offset += 1
        session.add(db.Evidence(root_id="old", source_segment_id=b["segments"][0]["id"],
                                start_offset=0, end_offset=1, quote="错误"))
        key = session.get(db.SourceVersion, c["version_id"]).storage_key
    store.path(key).write_text("changed original")
    preview = cleaner.preview()
    assert len(preview["items"]) == 3
    assert all(not row["eligible"] and row["reason"] for row in preview["items"])
    assert store.path(key).exists()


def test_shared_original_and_retry_after_unlink(html_case, tmp_path, monkeypatch):
    from knowledgeops_agent.app.lifecycle import assets

    cleaner, sources, store = html_case
    first, second = old_web(html_case), old_web(html_case)
    entries = cleaner.preview()["items"]
    assert all(len(item["shared_with"]) == 1 for item in entries)
    backup = proof(tmp_path, cleaner, entries)
    jobs = [cleaner.admit(item, backup_folder=backup) for item in entries]
    assert cleaner.step(identity=jobs[0]["id"])["phase"] == "html_files"
    assert cleaner.step(identity=jobs[0]["id"])["summary"]["shared_file_retained"]
    assert cleaner.step(identity=jobs[1]["id"])["phase"] == "html_files"
    unlink = assets._unlink

    def fail_after_unlink(*args):
        unlink(*args)
        raise OSError("crash after unlink")

    monkeypatch.setattr(assets, "_unlink", fail_after_unlink)
    assert cleaner.step(identity=jobs[1]["id"])["status"] == "failed"
    assert not store.path(entries[1]["storage_key"]).exists()
    # 重试间隙真实上传相同字节成为文件材料，仍需要自己的原件。
    uploaded = sources.import_text("用户HTML文件", "上传正文", kind="file", raw_bytes=b"<html>original HTML</html>", mime_type="text/html")
    monkeypatch.setattr(assets, "_unlink", unlink)
    cleaner.jobs.retry(jobs[1]["id"])
    assert cleaner.step(identity=jobs[1]["id"])["status"] == "completed"
    assert sources.original(uploaded["version_id"])[0] == b"<html>original HTML</html>"
    assert sources.full_content(first["version_id"])["text"] == sources.full_content(second["version_id"])["text"]


def test_late_body_change_blocks_detach(html_case, tmp_path):
    cleaner, _, store = html_case
    item = old_web(html_case)
    entry = cleaner.preview()["items"][0]
    job = cleaner.admit(entry, backup_folder=proof(tmp_path, cleaner, [entry]))
    with cleaner.sessions.begin() as session:
        piece = session.get(db.SourceSegment, item["segments"][0]["id"])
        piece.text = "修改正文仍完整。"
        piece.end_offset = len(piece.text)
    assert cleaner.step(identity=job["id"])["status"] == "failed"
    assert store.path(entry["storage_key"]).exists()
    with cleaner.sessions() as session:
        assert session.get(db.SourceVersion, item["version_id"]).storage_key is not None


def test_preview_paginates_and_does_not_touch_new_web_or_uploaded_html(html_case):
    cleaner, sources, _ = html_case
    for _ in range(3):
        old_web(html_case)
    sources.import_text("新网页", "# 新正文", kind="web", origin="https://example.org/new")
    sources.import_text("用户上传", "上传正文", kind="file", raw_bytes=b"<html>uploaded</html>", mime_type="text/html")
    one = cleaner.preview(limit=2)
    two = cleaner.preview(limit=2, after=one["next_cursor"])
    assert len(one["items"]) == 2 and len(two["items"]) == 1 and two["next_cursor"] is None
