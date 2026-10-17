"""旧引用分批纳管。文件正文必须实际读取，不能把只有路径当作已经核对引用。"""

import hashlib

import pytest
from sqlalchemy import select

from knowledgeops_agent.app.lifecycle.catalog import CATALOG_STATE, ReferenceCatalog
from knowledgeops_agent.persistence import models as db
from knowledgeops_agent.persistence.files import FileStore


def prepare(sessions):
    with sessions.begin() as session:
        identity = db.new_id()
        session.add(db.Run(id=identity, root_id=identity, checkpoint_id=identity, task="fixture", budgets={}))
        session.flush()
        artifact = db.RunArtifact(run_id=identity, kind="tool_result", content="body", content_hash="a" * 64)
        session.add(artifact)
        session.flush()
        chat = db.Conversation()
        session.add(chat)
        session.flush()
        for index in range(3):
            session.add(db.ChatMessage(conversation_id=chat.id, run_id=identity, role="user", content=f"{index}:{artifact.id}"))
        return artifact.id


def finish(catalog):
    processed = 0
    for _ in range(60):
        result = catalog.step(limit=1)
        processed += result["processed"]
        if result["complete"]:
            return processed
    pytest.fail("reference catalog did not converge")


def test_incremental_catalog_restarts_from_persisted_cursor_and_counts_actual_links(business_sessions):
    sessions = business_sessions
    artifact = prepare(sessions)
    catalog = ReferenceCatalog(sessions)
    with sessions() as session:
        assert not catalog.ready(session)
    assert catalog.step(limit=1) == {"processed": 1, "complete": False}
    assert finish(ReferenceCatalog(sessions)) == 4  # 两条消息、材料正文和活运行输入。
    with sessions() as session:
        assert catalog.ready(session)
        refs = list(session.scalars(select(db.ArtifactRetentionReference)))
        assert len(refs) == 3 and all(row.artifact_id == artifact and row.owner_kind == "message" for row in refs)
    assert catalog.step(limit=1) == {"processed": 0, "complete": True}


@pytest.mark.parametrize("resource", ["memory", "profile"])
def test_asset_edit_takes_control_lock_before_row_lock(business_sessions, tmp_path, resource):
    """真实两连接复现锁序：清理持控制锁时，编辑不能先占资产行再等控制锁。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread

    from sqlalchemy import event

    from knowledgeops_agent.app.lifecycle.catalog import install_reference_tracking
    from knowledgeops_agent.app.runs.control import lock_control
    from knowledgeops_agent.learning.profiles import LearningProfileService
    from knowledgeops_agent.memory.service import MemoryService

    sessions = business_sessions
    sessions.configure(info={"file_store": FileStore(tmp_path)})
    install_reference_tracking(sessions)
    if resource == "memory":
        service = MemoryService(sessions)
        value = service.create("先讲原理", scope="global", topic="")
        model = db.Preference
    else:
        service = LearningProfileService(sessions)
        value = service.get()
        model = db.LearningProfile
    waiting = Event()

    def about_to_lock(_conn, _cursor, statement, _params, _context, _many):
        if current_thread().name.startswith("asset-writer") and "pg_advisory_xact_lock" in statement:
            waiting.set()

    engine = sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", about_to_lock)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="asset-writer") as pool:
            with sessions.begin() as session:
                lock_control(session)
                future = pool.submit(service.update, value["id"], expected_revision=value["revision"], content="先建立框架")
                assert waiting.wait(5), "writer did not reach the shared control boundary"
                # 旧锁序在此报LockNotAvailable；不能以数据库随机裁决死锁当并发正确。
                assert session.scalar(select(model).where(model.id == value["id"]).with_for_update(nowait=True))
            assert future.result(timeout=5)["content"] == "先建立框架"
    finally:
        event.remove(engine, "before_cursor_execute", about_to_lock)


def test_file_asset_is_read_and_broken_file_blocks_catalog_completion(business_sessions, tmp_path):
    sessions = business_sessions
    store = FileStore(tmp_path)
    sessions.configure(info={"file_store": store})
    artifact = prepare(sessions)
    body = "以后查阅原始材料 " + artifact
    with sessions.begin() as session:
        profile = db.LearningProfile(identity_key="global")
        session.add(profile)
        session.flush()
        session.add(db.LearningProfileRevision(
            profile_id=profile.id, revision=1, storage_key="profiles/versions/fixed.md",
            sha256=hashlib.sha256(body.encode()).hexdigest(), source={}, enabled=True,
        ))
    catalog = ReferenceCatalog(sessions)
    with pytest.raises(FileNotFoundError):
        finish(catalog)
    with sessions() as session:
        assert not catalog.ready(session)
        saved_cursor = session.get(db.MaintenanceState, CATALOG_STATE).cursor
    store.put("profiles/versions/fixed.md", body)
    finish(catalog)
    with sessions() as session:
        assert catalog.ready(session)
        assert saved_cursor["table"] < session.get(db.MaintenanceState, CATALOG_STATE).cursor["table"]
        profile_links = list(session.scalars(select(db.ArtifactRetentionReference).where(db.ArtifactRetentionReference.owner_kind == "asset")))
        assert len(profile_links) == 1 and profile_links[0].artifact_id == artifact
