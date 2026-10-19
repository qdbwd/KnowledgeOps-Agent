"""文件安装后DB回滚的窗口，用真实存储、事务和维护入口验证。"""

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import select
from test_lifecycle_details import checkpoint_case as _checkpoint_case
from test_lifecycle_details import detail_case as _detail_case
from test_lifecycle_details import ledger as _ledger
from test_lifecycle_details import redis_case as _redis_case

from knowledgeops_agent.app.lifecycle.installed_content import collect_installations
from knowledgeops_agent.app.lifecycle.maintenance import SCAN_STATE, LifecycleMaintenance
from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.persistence import models as db
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.persistence.installations import DIRECTORY, InstallationLease
from knowledgeops_agent.sources.deliveries import DeliveryService
from knowledgeops_agent.sources.service import SourceService

detail_case, ledger, checkpoint_case, redis_case = _detail_case, _ledger, _checkpoint_case, _redis_case


def scan_content(details, root):
    with details.sessions.begin() as session:
        row = session.get(db.MaintenanceState, SCAN_STATE)
        if row is None:
            row = db.MaintenanceState(name=SCAN_STATE)
            session.add(row)
        row.cursor, row.next_scan_at = {"phase": "content", "root": 0}, details.now()
    result = LifecycleMaintenance(details, temporary_roots=[root]).scan_step()
    assert not result.get("error"), result
    return result


def test_rolled_back_upload_is_collected_without_adopting_old_unknown_files(detail_case, tmp_path, monkeypatch):
    details = detail_case[0]
    source_store = FileStore(tmp_path / "uploads")
    details.sessions.configure(info={"source_store": source_store})
    sources = SourceService(details.sessions, source_store.root)
    old_digest = source_store.put("old-unregistered.bin", "keep unknown")
    old_key = f"{old_digest[:2]}/{old_digest}"
    source_store.put(old_key, "keep unknown")  # 即使外观像内容对象，也没有安装登记。
    body = "failed import after file installation"
    digest = hashlib.sha256(body.encode()).hexdigest()
    key = f"{digest[:2]}/{digest}"
    original = sources._version_view

    def abort(*_):
        raise RuntimeError("simulated database rollback")

    monkeypatch.setattr(sources, "_version_view", abort)
    with pytest.raises(RuntimeError, match="rollback"):
        sources.import_text("not committed", body)
    assert source_store.path(key).is_file()
    with details.sessions() as session:
        assert session.scalar(select(db.SourceVersion.id)) is None
    monkeypatch.setattr(sources, "_version_view", original)
    result = scan_content(details, source_store.root)
    assert result["removed"] == 1
    assert not source_store.path(key).exists()
    assert source_store.read(old_key, old_digest) == "keep unknown"
    assert source_store.path("old-unregistered.bin").exists()


@pytest.fixture
def content_case(business_sessions, tmp_path):
    workspace, uploads = FileStore(tmp_path / "workspace"), FileStore(tmp_path / "uploads")
    business_sessions.configure(info={"file_store": workspace, "source_store": uploads})

    def sweep(store=workspace, **kwargs):
        with business_sessions.begin() as session:
            lock_control(session)
            return collect_installations(session, store.root, now=db.utcnow() + timedelta(days=2), **kwargs)

    return business_sessions, workspace, uploads, sweep


def test_committed_import_keeps_original_and_releases_journal(content_case, monkeypatch):
    sessions, _, uploads, sweep = content_case
    sources = SourceService(sessions, uploads.root)
    body = "same bytes after rollback"
    original = sources._version_view
    monkeypatch.setattr(sources, "_version_view", lambda *_: (_ for _ in ()).throw(RuntimeError("abort")))
    with pytest.raises(RuntimeError):
        sources.import_text("first failed", body)
    monkeypatch.setattr(sources, "_version_view", original)
    item = sources.import_text("retry succeeded", body)
    assert list((uploads.root / DIRECTORY).glob("*.json"))
    assert sweep(uploads)["registered"] == 1
    assert sources.original(item["version_id"])[0] == body.encode()
    assert not list((uploads.root / DIRECTORY).glob("*.json"))
    assert sweep(uploads)["removed"] == 0


def test_delivery_rollback_and_concurrent_registration_keep_work_file(content_case, monkeypatch):
    from knowledgeops_agent.sources import deliveries as module

    sessions, store, _, sweep = content_case
    deliveries = DeliveryService(sessions, store.root)
    store.put("outputs/shared.txt", "shared delivery bytes")
    with pytest.raises(RuntimeError, match="abort"):
        deliveries.present(["outputs/shared.txt"], run_id="r", root_id="r", call_id="failed",
            commit_guard=lambda _: (_ for _ in ()).throw(RuntimeError("abort")))
    digest = hashlib.sha256(b"shared delivery bytes").hexdigest()
    blob = store.path(f"deliveries/blobs/{digest[:2]}/{digest}")
    assert blob.exists()
    installed, resume = Event(), Event()
    original = module.hold_blob

    def hold(*args):
        original(*args)
        installed.set()
        assert resume.wait(15), "collector blocked an installer that needs the DB lock"

    monkeypatch.setattr(module, "hold_blob", hold)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(deliveries.present, ["outputs/shared.txt"], run_id="r", root_id="r", call_id="retry")
        assert installed.wait(15)
        try:
            result = sweep()
            assert result["busy"] and blob.exists()
        finally:
            resume.set()
        item = future.result(timeout=15)[0]
    assert sweep()["registered"] >= 1
    assert deliveries.read(item["id"])[1] == b"shared delivery bytes"
    assert store.read("outputs/shared.txt", digest) == "shared delivery bytes"


def test_transaction_and_savepoint_hold_installation_until_outer_rollback(content_case):
    sessions, store, _, sweep = content_case
    key = f"knowledge/versions/{db.new_id()}.md"
    with sessions() as session:
        transaction = session.begin()
        store.put(key, "not committed", session=session, purpose="knowledge")
        with session.begin_nested():
            pass
        assert sweep()["busy"]
        transaction.rollback()
    assert sweep()["removed"] == 1
    assert not store.path(key).exists()


def test_process_death_releases_lease_and_missing_file_retry_finishes(content_case, monkeypatch):
    from knowledgeops_agent.app.lifecycle import installed_content

    _, store, _, sweep = content_case
    key = f"knowledge/versions/{db.new_id()}.md"
    code = """
import hashlib, os, sys
from pathlib import Path
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.persistence.installations import InstallationLease
store = FileStore(Path(sys.argv[1]))
lease = InstallationLease(store)
lease.record('knowledge', sys.argv[2], hashlib.sha256(b'crashed').hexdigest())
store.put(sys.argv[2], 'crashed')
os._exit(17)
"""
    process = subprocess.run([sys.executable, "-c", code, str(store.root), key], timeout=20, check=False)
    assert process.returncode == 17 and store.path(key).is_file()
    unlink = installed_content._unlink

    def crash_after_delete(*args):
        unlink(*args)
        raise OSError("crashed after unlink")

    monkeypatch.setattr(installed_content, "_unlink", crash_after_delete)
    assert sweep()["unrecognized"] == 1
    assert not store.path(key).exists() and list((store.root / DIRECTORY).glob("*.json"))
    monkeypatch.setattr(installed_content, "_unlink", unlink)
    assert sweep()["unrecognized"] == 0
    assert not list((store.root / DIRECTORY).glob("*.json"))


def test_changed_content_and_unsafe_records_are_preserved_with_bounded_cursor(content_case):
    sessions, store, _, sweep = content_case
    keys = [f"knowledge/versions/{db.new_id()}.md" for _ in range(3)]
    with sessions() as session:
        session.begin()
        for key in keys:
            store.put(key, "expected", session=session, purpose="knowledge")
        session.rollback()
    store.path(keys[0]).write_text("manual changed content")
    first = sweep(limit=2)
    assert first["next_cursor"]
    second = sweep(limit=2, after=first["next_cursor"])
    assert second["next_cursor"] is None
    assert first["removed"] + second["removed"] == 2
    assert first["unrecognized"] + second["unrecognized"] == 1
    record = next((store.root / DIRECTORY).glob("*.json"))
    metadata = json.loads(record.read_text())
    metadata["key"] = "outputs/user.txt"
    store.put("outputs/user.txt", "expected")
    record.write_text(json.dumps(metadata))
    assert sweep()["unrecognized"] == 1
    assert store.path("outputs/user.txt").is_file()
    record.unlink()
    # 日志/目标上的符号链接和FIFO也不能变成任意文件删除或阻塞读取。
    with InstallationLease(store) as lease:
        key = f"knowledge/versions/{db.new_id()}.md"
        digest = hashlib.sha256(b"expected").hexdigest()
        lease.record("knowledge", key, digest)
        store.path(key).symlink_to(store.path("outputs/user.txt"))
    assert sweep()["unrecognized"] == 1
    assert store.path("outputs/user.txt").read_text() == "expected"
    (store.root / key).unlink()
    os.mkfifo(store.root / key)
    assert sweep()["unrecognized"] == 1


def test_grace_interval_keeps_new_orphan(content_case):
    sessions, store, _, _ = content_case
    key = f"knowledge/versions/{db.new_id()}.md"
    with sessions() as session:
        session.begin()
        store.put(key, "young", session=session, purpose="knowledge")
        session.rollback()
    with sessions.begin() as session:
        result = collect_installations(session, store.root, now=db.utcnow())
    assert result["protected"] == 1 and store.path(key).is_file()
