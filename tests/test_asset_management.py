"""单份资产移除以真实引用为准，文件回收可重试且不会误删共享内容。"""

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import select

from knowledgeops_agent.app.lifecycle.asset_cleanup import AssetCleaner
from knowledgeops_agent.app.lifecycle.catalog import ReferenceCatalog, install_reference_tracking
from knowledgeops_agent.app.lifecycle.jobs import CleanupJobs
from knowledgeops_agent.app.runs.service import RunService
from knowledgeops_agent.persistence import models as db
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.sources.deliveries import DeliveryService
from knowledgeops_agent.sources.service import SourceService


@pytest.fixture
def assets(business_sessions, tmp_path):
    workspace, uploads = FileStore(tmp_path / "workspace"), FileStore(tmp_path / "uploads")
    business_sessions.configure(info={"file_store": workspace, "source_store": uploads})
    install_reference_tracking(business_sessions)
    cleaner = AssetCleaner(business_sessions, CleanupJobs(business_sessions), batch_size=2)
    catalog = ReferenceCatalog(business_sessions)
    for _ in range(100):
        if catalog.step(limit=100)["complete"]:
            break
    else:
        pytest.fail("reference catalog did not finish")
    return cleaner, SourceService(business_sessions, uploads.root), DeliveryService(business_sessions, workspace.root)


def drain(cleaner, job):
    for _ in range(50):
        job = cleaner.jobs.get(job["id"])
        if job["status"] in {"completed", "failed"}:
            return job
        cleaner.step(identity=job["id"])
    pytest.fail("asset deletion did not finish")


def test_remove_library_preserves_other_owner_and_shared_original(assets):
    cleaner, sources, _ = assets
    chat = RunService(cleaner.sessions).create_conversation("仍在使用")
    shared = sources.import_text("共享资料", "共享原件", origin="fixture:shared")
    sources.retain_source(shared["id"], conversation_id=chat["id"])
    preview = cleaner.preview("source", shared["id"])
    assert preview["counts"] == {"delete": 0, "keep": 1}
    job = cleaner.admit("source", shared["id"], expected_version=preview["version"])
    original_files = set(sources.storage_root.rglob("*"))
    with pytest.raises(ValueError, match="正在从这个列表移除"):
        sources.import_text("尚未接受的新版", "must not create an orphan", origin="fixture:shared")
    assert set(sources.storage_root.rglob("*")) == original_files
    assert drain(cleaner, job)["status"] == "completed"
    assert sources.list_sources() == []
    assert sources.list_sources(conversation_id=chat["id"], include_library=False)[0]["id"] == shared["id"]
    assert sources.original(shared["version_id"])[0] == "共享原件".encode()
    # 再移除会话的使用关系，已经没有根引用才真正释放。
    preview = cleaner.preview("source", shared["id"], conversation_id=chat["id"])
    assert preview["counts"] == {"delete": 1, "keep": 0}
    job = cleaner.admit("source", shared["id"], expected_version=preview["version"], conversation_id=chat["id"])
    assert drain(cleaner, job)["status"] == "completed"
    with cleaner.sessions() as session:
        assert session.get(db.Source, shared["id"]) is None


def test_stale_preview_and_late_real_reference_keep_fixed_body(assets):
    cleaner, sources, _ = assets
    source = sources.import_text("原资料", "被正式使用的正文")
    preview = cleaner.preview("source", source["id"])
    other = sources.import_text("引用资料", f"[引用](/sources/{source['id']}?version={source['version_id']})")
    with pytest.raises(ValueError, match="状态已变化"):
        cleaner.admit("source", source["id"], expected_version=preview["version"])
    # 移除该引用，先接受删除；随后创建新的真实依赖，执行阶段仍会重算。
    with cleaner.sessions.begin() as session:
        session.delete(session.get(db.SourceUse, (other["id"], "library", "default")))
    preview = cleaner.preview("source", source["id"])
    job = cleaner.admit("source", source["id"], expected_version=preview["version"])
    sources.import_text("后来引用", f"[旧正文](/sources/{source['id']}?version={source['version_id']})")
    assert drain(cleaner, job)["summary"]["kept"] == 1
    assert sources.full_content(source["version_id"])["text"] == "被正式使用的正文"


def test_delivery_removal_preserves_reference_and_fixed_download(assets):
    cleaner, sources, deliveries = assets
    deliveries.store.put("outputs/报告.md", "# 固定成果")
    item = deliveries.present(["outputs/报告.md"], run_id="run", root_id="root", call_id="call")[0]
    sources.import_text("保留链接", f"[成果]({item['view_url']})")
    preview = cleaner.preview("delivery", item["id"])
    assert preview["counts"] == {"delete": 0, "keep": 1}
    result = drain(cleaner, cleaner.admit("delivery", item["id"], expected_version=preview["version"]))
    assert result["status"] == "completed"
    assert deliveries.list() == []
    assert deliveries.read(item["id"])[1] == "# 固定成果".encode()
    assert deliveries.present(["outputs/报告.md"], run_id="run", root_id="root", call_id="call")[0]["id"] == item["id"]
    assert deliveries.list() == []  # 幂等回执不会把用户已移除的资产重新登记。


def test_delivery_files_retry_and_protect_new_shared_registration(assets, monkeypatch):
    from knowledgeops_agent.app.lifecycle import asset_cleanup

    cleaner, _, deliveries = assets
    deliveries.store.put("outputs/共享.txt", "identical")
    item = deliveries.present(["outputs/共享.txt"], run_id="r", root_id="r", call_id="one")[0]
    preview = cleaner.preview("delivery", item["id"])
    job = cleaner.admit("delivery", item["id"], expected_version=preview["version"])
    original = asset_cleanup._unlink

    def fail_after_unlink(*args):
        original(*args)
        raise OSError("simulated crash")

    monkeypatch.setattr(asset_cleanup, "_unlink", fail_after_unlink)
    assert drain(cleaner, job)["status"] == "failed"
    with cleaner.sessions() as session:
        assert session.get(db.DeliveredFile, item["id"]) is None
    replacement = deliveries.present(["outputs/共享.txt"], run_id="r", root_id="r", call_id="two")[0]
    monkeypatch.setattr(asset_cleanup, "_unlink", original)
    cleaner.jobs.retry(job["id"])
    assert drain(cleaner, job)["status"] == "completed"
    assert deliveries.read(replacement["id"])[1] == b"identical"
    with cleaner.sessions() as session:
        assert list(session.scalars(select(db.DeliveredFile.id))) == [replacement["id"]]


def test_last_reference_disappears_sweeper_collects_both_assets_but_not_unknown_legacy(assets):
    cleaner, sources, deliveries = assets
    deliveries.store.put("outputs/orphan.txt", "delivered")
    item = deliveries.present(["outputs/orphan.txt"], run_id="r", root_id="r", call_id="sweep")[0]
    source = sources.import_text("将失去引用", "temporary body")
    legacy = sources.import_text("旧意图未知", "keep legacy")
    with cleaner.sessions.begin() as session:
        session.get(db.Source, legacy["id"]).ownership_known = False
        session.delete(session.get(db.SourceUse, (legacy["id"], "library", "default")))
    root = sources.import_text("使用者", f"[正文](/sources/{source['id']}) [文件]({item['view_url']})")
    for kind, identity in (("source", source["id"]), ("delivery", item["id"])):
        preview = cleaner.preview(kind, identity)
        assert drain(cleaner, cleaner.admit(kind, identity, expected_version=preview["version"]))["summary"]["kept"] == 1
    preview = cleaner.preview("source", root["id"])
    assert drain(cleaner, cleaner.admit("source", root["id"], expected_version=preview["version"]))["status"] == "completed"
    cursor, count = None, 0
    for _ in range(10):
        result = cleaner.reconcile(after=cursor, limit=1)
        count += result["scheduled"]
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert count == 2
    for job in cleaner.list_jobs():
        assert drain(cleaner, job)["status"] == "completed"
    with cleaner.sessions() as session:
        assert session.get(db.Source, source["id"]) is None
        assert session.get(db.DeliveredFile, item["id"]) is None
        assert session.get(db.Source, legacy["id"]) is not None


def test_active_conversation_attachment_cannot_be_detached_before_pending_execution(assets):
    cleaner, sources, _ = assets
    runs = RunService(cleaner.sessions)
    chat = runs.create_conversation("尚未完成")
    source = sources.import_text("执行输入", "task input", conversation_id=chat["id"])
    runs.enqueue(chat["id"], "分析附件", request_key="pending")
    preview = cleaner.preview("source", source["id"], conversation_id=chat["id"])
    assert not preview["eligible"]
    with pytest.raises(ValueError, match="所属任务"):
        cleaner.admit("source", source["id"], conversation_id=chat["id"], expected_version=preview["version"])


def test_temporary_drafts_are_collected_only_after_last_real_dependency(assets):
    cleaner, sources, _ = assets
    with cleaner.sessions.begin() as session:
        run_id = db.new_id()
        session.add(db.Run(id=run_id, root_id=run_id, checkpoint_id=run_id, task="finished", budgets={},
                           status="completed", lifecycle_state="expired"))
        session.flush()
        drafts = [db.ChangeSet(root_id=run_id, operations=[{"body": "draft"}], retention_intent=intent)
                  for intent in ("temporary", "temporary", "saved", "legacy")]
        unknown = db.ChangeSet(root_id="unknown-owner", operations=[], retention_intent="temporary")
        session.add_all([*drafts, unknown])
        session.flush()
        ids = [row.id for row in drafts]
        unknown_id = unknown.id
    reference = sources.import_text("真实引用草稿", f"[待使用草稿](/changes/{ids[0]})")
    cleaner.reconcile(limit=20)
    # 作业接受后出现的新保存意图必须再次检查，不沿旧计划删除。
    with cleaner.sessions.begin() as session:
        session.get(db.ChangeSet, ids[1]).retention_intent = "saved"
    for job in cleaner.list_jobs():
        assert drain(cleaner, job)["status"] == "completed"
    with cleaner.sessions() as session:
        assert all(session.get(db.ChangeSet, identity) is not None for identity in [*ids, unknown_id])
    with cleaner.sessions.begin() as session:
        session.delete(session.get(db.SourceUse, (reference["id"], "library", "default")))
    cleaner.reconcile(limit=20)
    for job in cleaner.list_jobs():
        assert drain(cleaner, job)["status"] == "completed"
    with cleaner.sessions() as session:
        assert session.get(db.ChangeSet, ids[0]) is None
        assert all(session.get(db.ChangeSet, identity) is not None for identity in [*ids[1:], unknown_id])


def test_blob_registration_and_cleanup_lock_order_does_not_deadlock(assets, monkeypatch):
    from knowledgeops_agent.sources import deliveries as module

    cleaner, _, deliveries = assets
    deliveries.store.put("outputs/race.txt", "shared blob")
    item = deliveries.present(["outputs/race.txt"], run_id="r", root_id="r", call_id="old")[0]
    preview = cleaner.preview("delivery", item["id"])
    job = cleaner.admit("delivery", item["id"], expected_version=preview["version"])
    assert cleaner.step(identity=job["id"])["phase"] == "asset_files"
    holding, proceed = Event(), Event()
    original = module.hold_blob

    def hold(*args):
        original(*args)
        holding.set()
        assert proceed.wait(15), "cleanup waited while holding the database lock"

    monkeypatch.setattr(module, "hold_blob", hold)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(deliveries.present, ["outputs/race.txt"], run_id="r", root_id="r", call_id="new")
        assert holding.wait(15)
        try:
            blocked = cleaner.step(identity=job["id"])
            assert blocked["status"] == "failed" and blocked["last_error"] == "BlockingIOError"
        finally:
            proceed.set()
        replacement = pending.result(timeout=15)[0]
    cleaner.jobs.retry(job["id"])
    assert drain(cleaner, job)["status"] == "completed"
    assert deliveries.read(replacement["id"])[1] == b"shared blob"


@pytest.mark.parametrize("tamper", ["symlink", "fifo"])
def test_delivery_cleanup_rejects_nonregular_files_without_hanging_or_following_links(assets, tamper):
    cleaner, _, deliveries = assets
    deliveries.store.put("outputs/boundary.txt", "body")
    item = deliveries.present(["outputs/boundary.txt"], run_id="r", root_id="r", call_id="boundary")[0]
    with cleaner.sessions() as session:
        target = deliveries.store.root / session.get(db.DeliveredFile, item["id"]).storage_key
    preview = cleaner.preview("delivery", item["id"])
    job = cleaner.admit("delivery", item["id"], expected_version=preview["version"])
    assert cleaner.step(identity=job["id"])["phase"] == "asset_files"
    target.unlink()
    external = deliveries.store.root.parent / "external.txt"
    if tamper == "fifo":
        os.mkfifo(target)
    else:
        external.write_text("external survives")
        target.symlink_to(external)
    failed = cleaner.step(identity=job["id"])
    assert failed["status"] == "failed" and failed["last_error"] == "ValueError"
    if tamper == "fifo":
        assert stat.S_ISFIFO(target.lstat().st_mode)
    else:
        assert external.read_text() == "external survives" and target.is_symlink()
    target.unlink()
    target.write_text("body")
    cleaner.jobs.retry(job["id"])
    assert drain(cleaner, job)["status"] == "completed"
