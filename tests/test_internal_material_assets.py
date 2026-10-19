"""内部文本只按实际用途保留；正式引用转换后仍能读旧地址，内部副本可释放。"""

import hashlib
import json

import pytest
from sqlalchemy import select
from test_lifecycle_details import checkpoint_case as _checkpoint_case
from test_lifecycle_details import detail_case as _detail_case
from test_lifecycle_details import drain
from test_lifecycle_details import ledger as _ledger
from test_lifecycle_details import redis_case as _redis_case
from test_lifecycle_materials import artifact, finish

from knowledgeops_agent.app.lifecycle.access import DetailsExpired
from knowledgeops_agent.app.lifecycle.asset_cleanup import AssetCleaner
from knowledgeops_agent.app.lifecycle.materials import RunRetentionCleaner
from knowledgeops_agent.persistence import models as db
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.sources.service import SourceService

detail_case, ledger, checkpoint_case, redis_case = _detail_case, _ledger, _checkpoint_case, _redis_case


def link(run_id, artifact_id):
    return f"/runs/{run_id}/artifacts/{artifact_id}"


def setup(details, tmp_path):
    store = FileStore(tmp_path / "workspace")
    uploads = FileStore(tmp_path / "uploads")
    details.sessions.configure(info={"file_store": store, "source_store": uploads})
    return store, SourceService(details.sessions, uploads.root), RunRetentionCleaner(details)


def test_catalog_ids_and_incidental_uuids_do_not_retain_internal_text(detail_case, tmp_path):
    details, _, run, chat, *_ = detail_case
    store, sources, retained = setup(details, tmp_path)
    with details.sessions.begin() as session:
        row = artifact(session, store, run["id"], "不再使用的内部结果")
        identity, path = row.id, store.path(row.content_storage_key)
        snapshot = session.get(db.ContinuationSnapshot, session.get(db.Conversation, chat["id"]).latest_snapshot_id)
        snapshot.content = {**snapshot.content, "artifact_ids": [identity]}
    sources.import_text("运行编号记录", f"记录了一个编号 {identity}，没有引用其正文。")
    job = details.admit(run["id"])
    drain(details, job["id"])
    result = finish(retained, job["id"])
    assert result["status"] == "completed", result
    with details.sessions() as session:
        assert session.get(db.RunArtifact, identity).expired_at is not None
    assert not path.exists()


def test_referenced_text_becomes_fixed_source_and_old_read_permissions_remain(detail_case, tmp_path):
    details, runs, run, chat, *_ = detail_case
    store, sources, retained = setup(details, tmp_path)
    content = "固定文本\n" * 1500
    with details.sessions.begin() as session:
        row = artifact(session, store, run["id"], content)
        identity, path = row.id, store.path(row.content_storage_key)
    reference = sources.import_text("正式使用的资料", f"[核对全文]({link(run['id'], identity)})")
    job = details.admit(run["id"])
    drain(details, job["id"])
    result = finish(retained, job["id"])
    assert result["status"] == "completed", result
    with details.sessions() as session:
        row = session.get(db.RunArtifact, identity)
        assert row.expired_at is not None and row.promoted_version_id
        version_id = row.promoted_version_id
        version = session.get(db.SourceVersion, version_id)
        assert version.body_hash == hashlib.sha256(content.encode()).hexdigest()
        assert version.storage_key is None
        assert list(session.scalars(select(db.SourceUse).where(db.SourceUse.source_id == version.source_id))) == []
    assert not path.exists()
    assert sources.full_content(version_id)["text"] == content
    assert runs.read_artifact(identity, root_id=run["id"], start=6000)["text"] == content[6000:]
    unrelated = runs.enqueue(runs.create_conversation("其他会话")["id"], "无访问授权", request_key=db.new_id())
    with pytest.raises(PermissionError):
        runs.read_artifact(identity, root_id=unrelated["id"])
    # 最后一个真实依赖消失后，转换出的正文也不能凭别名或自动保存意图永久保留。
    with details.sessions.begin() as session:
        session.delete(session.get(db.SourceUse, (reference["id"], "library", "default")))
    assets = AssetCleaner(details.sessions, details.jobs, batch_size=10)
    assets.reconcile(limit=20)
    for _ in range(60):
        if assets.step() is None:
            break
    with details.sessions() as session:
        assert session.get(db.SourceVersion, version_id) is None
        assert session.get(db.RunArtifact, identity).promoted_version_id is None
    with pytest.raises(DetailsExpired):
        runs.read_artifact(identity, root_id=run["id"])


def test_corrupt_internal_body_cannot_be_promoted_or_silently_deleted(detail_case, tmp_path):
    details, _, run, *_ = detail_case
    store, sources, retained = setup(details, tmp_path)
    with details.sessions.begin() as session:
        row = artifact(session, store, run["id"], "原文")
        identity, path = row.id, store.path(row.content_storage_key)
    sources.import_text("需要核对的资料", f"[原文]({link(run['id'], identity)})")
    # 先完成引用回填，随后模拟外部修改；转换阶段也必须再次核对内容。
    while not retained.catalog.step(limit=100)["complete"]:
        pass
    path.write_text("外部改写", encoding="utf-8")
    job = details.admit(run["id"])
    drain(details, job["id"])
    result = finish(retained, job["id"])
    assert result["status"] == "failed" and result["phase"] == "retained_artifacts", result
    assert path.read_text() == "外部改写"
    with details.sessions() as session:
        row = session.get(db.RunArtifact, identity)
        assert row.expired_at is None and row.promoted_version_id is None


def test_nested_json_reference_survives_conversion_and_catalog_rebuild(detail_case, tmp_path):
    from knowledgeops_agent.app.lifecycle.asset_references import reachable_assets
    from knowledgeops_agent.app.lifecycle.catalog import CATALOG_STATE

    details, _, run, *_ = detail_case
    store, sources, retained = setup(details, tmp_path)
    with details.sessions.begin() as session:
        nested = artifact(session, store, run["id"], "深层固定正文")
        structured = json.dumps({"padding": "x" * 5000, "result": {"artifact_id": nested.id}})
        parent = artifact(session, store, run["id"], structured)
        identities = nested.id, parent.id
    sources.import_text("结构化引用", f"[结果]({link(run['id'], identities[1])})")
    job = details.admit(run["id"])
    drain(details, job["id"])
    assert finish(retained, job["id"])["status"] == "completed"
    with details.sessions.begin() as session:
        versions = [session.get(db.RunArtifact, identity).promoted_version_id for identity in identities]
        assert all(versions)
        # 派生索引可以完全重建，不能只靠转换时临时复制的边保护下一层。
        from sqlalchemy import delete
        session.execute(delete(db.AssetReference))
        session.delete(session.get(db.MaintenanceState, CATALOG_STATE))
    while not retained.catalog.step(limit=100)["complete"]:
        pass
    with details.sessions() as session:
        graph = reachable_assets(session)
        nodes = set(session.scalars(select(graph.c.node)))
        assert all("source_version:" + version in nodes for version in versions)
    assert sources.full_content(versions[0])["text"] == "深层固定正文"


def test_explicit_physical_path_keeps_file_until_that_dependency_disappears(detail_case, tmp_path):
    from knowledgeops_agent.app.lifecycle.maintenance import LifecycleMaintenance

    details, _, run, *_ = detail_case
    store, _, retained = setup(details, tmp_path)
    with details.sessions.begin() as session:
        row = artifact(session, store, run["id"], "工作文件直接使用的原文")
        identity, key = row.id, row.content_storage_key
    store.put("work/reference.md", f"[本地文件](/{key})")
    job = details.admit(run["id"])
    drain(details, job["id"])
    assert finish(retained, job["id"])["status"] == "completed"
    with details.sessions() as session:
        assert session.get(db.RunArtifact, identity).expired_at is None
    assert store.path(key).read_text() == "工作文件直接使用的原文"
    store.replace("work/reference.md", b"The link was removed.")
    LifecycleMaintenance(details)._reconcile(run["id"])
    assert finish(retained, job["id"])["status"] == "completed"
    assert not store.path(key).exists()
