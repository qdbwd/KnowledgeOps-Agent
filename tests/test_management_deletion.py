"""用户删除文章/运行：真实 PG、固定正文与引用闭包，不调用供应商。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import select
from test_lifecycle_details import checkpoint_case as _checkpoint_case
from test_lifecycle_details import detail_case as _detail_case
from test_lifecycle_details import ledger as _ledger
from test_lifecycle_details import redis_case as _redis_case

from knowledgeops_agent.app.lifecycle.catalog import ReferenceCatalog, install_reference_tracking
from knowledgeops_agent.app.lifecycle.jobs import CleanupJobs
from knowledgeops_agent.app.lifecycle.knowledge import KnowledgeCleaner
from knowledgeops_agent.knowledge.service import KnowledgeService
from knowledgeops_agent.persistence import models as db
from knowledgeops_agent.persistence.files import FileStore

checkpoint_case, detail_case, ledger, redis_case = _checkpoint_case, _detail_case, _ledger, _redis_case

@pytest.fixture
def articles(business_sessions, tmp_path):
    business_sessions.configure(info={"file_store": FileStore(tmp_path / "workspace")})
    install_reference_tracking(business_sessions)
    catalog = ReferenceCatalog(business_sessions)
    for _ in range(100):
        if catalog.step(limit=100)["complete"]:
            break
    service = KnowledgeService(business_sessions)
    cleaner = KnowledgeCleaner(business_sessions, CleanupJobs(business_sessions), batch_size=2)
    return service, cleaner


def publish(service, title="待删除文章", body="需要正确回收的正文", **operation):
    draft = service.create_draft("manual", [{"action": "create", "title": title, "body": body, **operation}])
    receipt = service.publish(draft["id"], idempotency_key=db.new_id(), user_approval=True,
                              expected_revision=draft["revision"])
    return service.get_document(draft["operations"][0]["document_id"]), receipt


def drain(cleaner, job):
    for _ in range(60):
        result = cleaner.jobs.get(job["id"])
        if result["status"] in {"completed", "failed"}:
            return result
        cleaner.step(identity=job["id"])
    pytest.fail("knowledge cleanup did not finish")


def remove(cleaner, identity):
    preview = cleaner.preview(identity)
    assert preview["eligible"], preview
    return cleaner.admit(identity, expected_version=preview["version"])


def test_delete_unreferenced_article_purges_body_index_working_file_and_prevents_revival(articles):
    service, cleaner = articles
    article, _ = publish(service)
    store = cleaner.sessions.kw["info"]["file_store"]
    with cleaner.sessions() as session:
        doc = session.get(db.KnowledgeDocument, article["id"])
        key = doc.workspace_path
        version_key = session.get(db.KnowledgeVersion, article["version_id"]).body_storage_key
    old_draft = service.create_draft("manual", [{"action": "update", "document_id": article["id"],
        "base_version_id": article["version_id"], "title": "旧修改", "body": "不可复活"}])
    # 丢弃的无会话草稿不应成为永久根。
    with cleaner.sessions.begin() as session:
        draft = session.get(db.ChangeSet, old_draft["id"])
        draft.retention_intent, draft.status = "temporary", "rejected"
    preview = cleaner.preview(article["id"])
    assert preview["counts"] == {"delete": 1, "keep": 0}
    job = cleaner.admit(article["id"], expected_version=preview["version"])
    assert service.list_documents(include_archived=True) == []
    assert service.search("正文") == []
    assert drain(cleaner, job)["status"] == "completed"
    assert not store.path(key).exists()
    assert not store.path(version_key).exists()
    with cleaner.sessions() as session:
        assert session.get(db.KnowledgeDocument, article["id"]).deleted_at is not None
        assert session.get(db.KnowledgeVersion, article["version_id"]) is None
        published = session.scalar(select(db.PublishReceipt).where(db.PublishReceipt.result["version_ids"].contains([article["version_id"]])))
        assert published is not None
        assert session.get(db.ChangeSet, published.changeset_id).operations == []
        assert list(session.scalars(select(db.ChangeSetRevision).where(db.ChangeSetRevision.changeset_id == published.changeset_id))) == []
    with pytest.raises(ValueError, match="已删除"):
        service.create_draft("manual", [{"action": "create", "document_id": article["id"], "title": "复活", "body": "禁止"}])
    assert cleaner.admit(article["id"], expected_version=preview["version"])["id"] == job["id"]


def test_fixed_reference_retains_only_used_version_then_reconciles(articles):
    service, cleaner = articles
    article, _ = publish(service)
    newer, _ = publish(service, title="新版本", body="最新正文", action="update",
        document_id=article["id"], base_version_id=article["version_id"])
    owner, _ = publish(service, title="引用者", body=f"[旧文](/knowledge/{article['id']}?version={article['version_id']})")
    preview = cleaner.preview(article["id"])
    assert preview["counts"] == {"delete": 1, "keep": 1}
    assert drain(cleaner, remove(cleaner, article["id"]))["status"] == "completed"
    assert service.get_document(article["id"], version_id=article["version_id"])["deleted_at"]
    with cleaner.sessions() as session:
        assert session.get(db.KnowledgeVersion, newer["version_id"]) is None
    assert drain(cleaner, remove(cleaner, owner["id"]))["status"] == "completed"
    cleaner.reconcile(limit=100)
    with cleaner.sessions() as session:
        job = session.scalar(select(db.CleanupJob).where(db.CleanupJob.target_id == article["id"]))
        identity = job.id
    assert drain(cleaner, {"id": identity})["status"] == "completed"
    with cleaner.sessions() as session:
        assert session.get(db.KnowledgeVersion, article["version_id"]) is None


def test_changed_preview_and_modified_working_file_are_protected(articles):
    service, cleaner = articles
    article, _ = publish(service)
    preview = cleaner.preview(article["id"])
    publish(service, title="新增引用", body=f"[链接](/knowledge/{article['id']})")
    with pytest.raises(ValueError, match="状态已变化"):
        cleaner.admit(article["id"], expected_version=preview["version"])
    job = remove(cleaner, article["id"])
    store = cleaner.sessions.kw["info"]["file_store"]
    with cleaner.sessions() as session:
        path = session.get(db.KnowledgeDocument, article["id"]).workspace_path
    store.replace(path, b"new external edit")
    assert drain(cleaner, job)["status"] == "failed"
    assert store.path(path).read_bytes() == b"new external edit"


def test_knowledge_evidence_keeps_versions(articles):
    service, cleaner = articles
    article, _ = publish(service)
    evidence = service.read_document(article["id"], root_id="manual")
    owner, _ = publish(service, title="证据引用", body=f"结论 [依据](/api/evidence/{evidence['id']})")
    assert cleaner.preview(article["id"])["counts"]["keep"] == 1
    assert drain(cleaner, remove(cleaner, article["id"]))["status"] == "completed"
    assert service.get_document(article["id"])["body"] == article["body"]
    assert drain(cleaner, remove(cleaner, owner["id"]))["status"] == "completed"


def test_manual_run_removal_preserves_chat_but_hides_record(detail_case):
    cleaner, runs, run, chat, clock, *_ = detail_case
    clock[0] -= timedelta(days=31)
    assert "retention_period" in cleaner.preview(run["id"])["blockers"]
    preview = cleaner.removal_preview(run["id"])
    job = cleaner.remove(run["id"], expected_version=preview["version"])
    assert job["summary"]["user_requested"] is True
    assert runs.list_runs() == []
    assert runs.get_run(run["id"])["user_deleted_at"]
    assert runs.messages(chat["id"])[-1]["content"] == "用户看到的最终回答"
    assert cleaner.remove(run["id"], expected_version=preview["version"])["id"] == job["id"]
    assert runs.enqueue(chat["id"], "继续聊天", request_key=db.new_id())["status"] == "queued"


def test_manual_run_removal_rechecks_unresolved_execution(detail_case):
    cleaner, _, run, *_ = detail_case
    preview = cleaner.removal_preview(run["id"])
    with cleaner.sessions.begin() as session:
        session.get(db.Run, run["id"]).status = "paused"
    with pytest.raises(ValueError):
        cleaner.remove(run["id"], expected_version=preview["version"])
    with cleaner.sessions() as session:
        assert session.get(db.Run, run["id"]).user_deleted_at is None


def test_saved_base_version_and_workspace_link_are_real_dependencies(articles):
    service, cleaner = articles
    article, _ = publish(service)
    draft = service.create_draft("manual", [{"action": "update", "document_id": article["id"],
        "base_version_id": article["version_id"], "title": "待确认修改", "body": "另一个版本"}], independently_saved=True)
    store = cleaner.sessions.kw["info"]["file_store"]
    store.put("notes/reference.md", f"[仍需使用](/knowledge/{article['id']})")
    assert cleaner.preview(article["id"])["counts"]["keep"] == 1
    job = remove(cleaner, article["id"])
    assert drain(cleaner, job)["status"] == "completed"
    assert service.get_draft(draft["id"])["before"][article["id"]]["body"] == article["body"]
    with pytest.raises(ValueError, match="已删除"):
        service.publish(draft["id"], idempotency_key=db.new_id(), user_approval=True, expected_revision=draft["revision"])


def test_catalog_must_be_complete_before_article_removal(articles):
    from knowledgeops_agent.app.lifecycle.catalog import CATALOG_STATE

    service, cleaner = articles
    article, _ = publish(service)
    with cleaner.sessions.begin() as session:
        session.delete(session.get(db.MaintenanceState, CATALOG_STATE))
    preview = cleaner.preview(article["id"])
    assert not preview["eligible"]
    with pytest.raises(ValueError, match="引用目录"):
        cleaner.admit(article["id"], expected_version=preview["version"])
    assert service.get_document(article["id"])["deleted_at"] is None


def test_file_cleanup_retry_uses_manifest_after_metadata_commit(articles, monkeypatch):
    from knowledgeops_agent.app.lifecycle import knowledge

    service, cleaner = articles
    article, _ = publish(service)
    job = remove(cleaner, article["id"])
    unlink = knowledge._unlink

    def crash_after_version_delete(store, key, digest):
        result = unlink(store, key, digest)
        if key.startswith("knowledge/versions/"):
            raise OSError("simulated interrupted cleanup")
        return result

    monkeypatch.setattr(knowledge, "_unlink", crash_after_version_delete)
    failed = drain(cleaner, job)
    assert failed["status"] == "failed" and failed["phase"] == "knowledge_files"
    with cleaner.sessions() as session:
        assert session.get(db.KnowledgeVersion, article["version_id"]) is None
    monkeypatch.setattr(knowledge, "_unlink", unlink)
    cleaner.jobs.retry(job["id"])
    assert drain(cleaner, job)["status"] == "completed"


def test_publish_and_delete_share_lock_and_revalidate(articles):
    service, cleaner = articles
    article, _ = publish(service)
    draft = service.create_draft("manual", [{"action": "update", "document_id": article["id"],
        "base_version_id": article["version_id"], "title": "并发修改", "body": "并发正文"}])
    preview = cleaner.preview(article["id"])
    barrier = Barrier(2)

    def attempt(action):
        barrier.wait(timeout=10)
        try:
            action()
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        deletion = pool.submit(attempt, lambda: cleaner.admit(article["id"], expected_version=preview["version"]))
        publication = pool.submit(attempt, lambda: service.publish(draft["id"], idempotency_key=db.new_id(),
            user_approval=True, expected_revision=draft["revision"]))
        assert sum((deletion.result(timeout=25), publication.result(timeout=25))) == 1


def test_manual_run_removal_applies_to_children_and_expired_root(detail_case):
    from knowledgeops_agent.app.lifecycle.maintenance import LifecycleMaintenance

    cleaner, runs, run, chat, clock, *_ = detail_case
    child_id = db.new_id()
    with cleaner.sessions.begin() as session:
        session.add(db.Run(id=child_id, root_id=run["id"], parent_id=run["id"], checkpoint_id=child_id,
            task="已交接子任务", input_data={}, budgets=run["budgets"], status="completed", finished_at=clock[0]))
    maintenance = LifecycleMaintenance(cleaner, enabled=False)
    for _ in range(100):
        if maintenance.retained.catalog.step(limit=100)["complete"]:
            break
    job = cleaner.admit(run["id"])
    for _ in range(150):
        if cleaner.jobs.get(job["id"])["status"] in {"completed", "failed"}:
            break
        maintenance.advance(job["id"])
    assert cleaner.jobs.get(job["id"])["status"] == "completed"
    assert runs.get_run(run["id"])["lifecycle_state"] == "expired"
    preview = cleaner.removal_preview(run["id"])
    assert preview["counts"]["delete"] == 2
    cleaner.remove(run["id"], expected_version=preview["version"])
    assert runs.get_run(child_id)["user_deleted_at"]
    assert runs.list_runs() == []
    assert runs.messages(chat["id"])
