"""回填完成后的新引用仍在所属事务中维护，材料之间的链接不成为永久根。"""

import hashlib
import json

import pytest
from langchain_core.messages import ToolMessage
from sqlalchemy import select
from test_lifecycle_catalog import finish, prepare

from knowledgeops_agent.app.lifecycle.catalog import ReferenceCatalog, install_reference_tracking
from knowledgeops_agent.app.lifecycle.references import mentioned_artifacts, reachable_artifacts
from knowledgeops_agent.persistence import models as db
from knowledgeops_agent.persistence.files import FileStore


def test_new_file_backed_asset_is_tracked_after_backfill_and_rollback_is_atomic(business_sessions, tmp_path):
    sessions = business_sessions
    store = FileStore(tmp_path)
    sessions.configure(info={"file_store": store})
    artifact = prepare(sessions)
    finish(ReferenceCatalog(sessions))
    install_reference_tracking(sessions)
    install_reference_tracking(sessions)  # 装配多个入口不能重复安装。
    body = f"参考 /runs/producer/artifacts/{artifact}"
    store.put("profiles/test.md", body)
    with sessions.begin() as session:
        profile = db.LearningProfile(identity_key="global")
        session.add(profile)
        session.flush()
        identity = profile.id
        session.add(db.LearningProfileRevision(profile_id=identity, revision=1,
                    storage_key="profiles/test.md", sha256=hashlib.sha256(body.encode()).hexdigest(), source={}, enabled=True))
    with sessions() as session:
        assert session.get(db.ArtifactRetentionReference, (artifact, "asset", f"profile:{identity}:1"))
    with pytest.raises(RuntimeError), sessions.begin() as session:
        session.delete(session.get(db.LearningProfileRevision, (identity, 1)))
        session.flush()
        assert session.get(db.ArtifactRetentionReference, (artifact, "asset", f"profile:{identity}:1")) is None
        raise RuntimeError("rollback")
    with sessions() as session:
        assert session.get(db.ArtifactRetentionReference, (artifact, "asset", f"profile:{identity}:1"))


def test_material_graph_preserves_transitive_links_but_not_an_unreachable_cycle(business_sessions):
    sessions = business_sessions
    install_reference_tracking(sessions)
    with sessions.begin() as session:
        run_id = db.new_id()
        session.add(db.Run(id=run_id, root_id=run_id, checkpoint_id=run_id, task="fixture", budgets={},
                          status="completed", lifecycle_state="expired"))
        session.flush()
        ids = [db.new_id() for _ in range(4)]
        for index, target in enumerate([ids[1], ids[0], ids[3], ids[2]]):
            body = f"/runs/{run_id}/artifacts/{target}"
            session.add(db.RunArtifact(id=ids[index], run_id=run_id, kind=str(index), content=body,
                                      content_hash=hashlib.sha256(body.encode()).hexdigest()))
        session.flush()
        memory = db.Preference(content=ids[0], scope="global")
        session.add(memory)
        session.flush()
        session.add(db.PreferenceRevision(preference_id=memory.id, revision=1, content={"content": f"/runs/{run_id}/artifacts/{ids[0]}"}))
    with sessions() as session:
        assert set(session.scalars(select(reachable_artifacts(session).c.id))) == set(ids[:2])
        assert len(list(session.scalars(select(db.ArtifactRetentionReference).where(
            db.ArtifactRetentionReference.owner_kind == "material",
        )))) == 4


def test_checkpoint_messages_and_internal_storage_paths_are_real_references(business_sessions, tmp_path):
    sessions = business_sessions
    store = FileStore(tmp_path)
    sessions.configure(info={"file_store": store})
    install_reference_tracking(sessions)
    with sessions.begin() as session:
        run_id, artifact_id = db.new_id(), db.new_id()
        session.add(db.Run(id=run_id, root_id=run_id, checkpoint_id=run_id, task="fixture", budgets={}))
        session.flush()
        digest = hashlib.sha256(b"body").hexdigest()
        key = f"artifacts/{run_id}/{digest}.txt"
        store.put(key, "body")
        session.add(db.RunArtifact(id=artifact_id, run_id=run_id, kind="text", content="body", content_hash=digest))
        session.flush()
        # 模拟旧路径链接；目标文件本身读取不在这个识别测试范围。
        row = session.get(db.RunArtifact, artifact_id)
        row.content_storage_key = key
        session.flush()
        from knowledgeops_agent.app.lifecycle.references import replace_references
        replace_references(session, "checkpoint", "consumer", {"path": "/" + key})
        assert session.get(db.ArtifactRetentionReference, (artifact_id, "checkpoint", "consumer"))
    assert mentioned_artifacts({"messages": [ToolMessage(json.dumps({"artifact_id": artifact_id}), tool_call_id="call")]}) == {artifact_id}
    assert mentioned_artifacts({"messages": [ToolMessage(artifact_id, tool_call_id="call")]}) == set()
    assert mentioned_artifacts(f"完整原始记录可用read_tool_result读取 {artifact_id}。") == {artifact_id}
    assert mentioned_artifacts({"artifact_ids": [artifact_id]}) == set()
    assert mentioned_artifacts(json.dumps({"material_catalog": [f"/runs/producer/artifacts/{artifact_id}"]})) == set()


def test_imported_source_link_split_at_segment_boundary_still_retains_original(business_sessions, tmp_path):
    from knowledgeops_agent.sources.service import SourceService
    sessions = business_sessions
    identity = prepare(sessions)
    install_reference_tracking(sessions)
    # import_text按2400字符分段，明确链接在两个片段之间被切开。
    SourceService(sessions, tmp_path).import_text("引用材料", "x" * 2360 + f" /runs/producer/artifacts/{identity}")
    with sessions() as session:
        assert len(list(session.scalars(select(db.ArtifactRetentionReference).where(
            db.ArtifactRetentionReference.artifact_id == identity,
            db.ArtifactRetentionReference.owner_kind == "asset",
            db.ArtifactRetentionReference.owner_id.like("source-segment:%"),
        )))) == 2
