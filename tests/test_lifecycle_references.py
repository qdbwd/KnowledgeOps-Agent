"""访问目录不应变成永久保留根；实际链接和笔记仍应保护原文。"""

from sqlalchemy import select

from knowledgeops_agent.app.lifecycle.references import replace_references
from knowledgeops_agent.persistence import models as db


def test_snapshot_catalog_does_not_pin_but_body_and_notes_do(business_sessions):
    with business_sessions.begin() as session:
        run = db.Run(id="root", root_id="root", checkpoint_id="root", task="refs", budgets={})
        session.add(run)
        session.flush()
        artifacts = [db.RunArtifact(run_id="root", kind="tool_result", content_hash=str(i) * 64, content="original") for i in (1, 2, 3)]
        session.add_all(artifacts)
        session.flush()
        used, note, catalog_only = [row.id for row in artifacts]
        snapshot = {"artifact_ids": [used, note, catalog_only],
                    "messages": [{"content": f"[引用](/api/runs/root/artifacts/{used})"}],
                    "task_notes": [{"content": "原始结果", "artifact_id": note}]}
        assert replace_references(session, "snapshot", "s1", snapshot, snapshot=True) == {used, note}
        # 更新只解除这个拥有者的引用，不删除其他拥有者的独立保护。
        replace_references(session, "message", "m1", {"artifact_id": used})
        assert replace_references(session, "snapshot", "s1", {"artifact_ids": [used, note]}, snapshot=True) == set()
    with business_sessions() as session:
        refs = list(session.scalars(select(db.ArtifactRetentionReference)))
        assert [(row.owner_kind, row.owner_id, row.artifact_id) for row in refs] == [("message", "m1", used)]


def test_repeated_reference_registration_is_idempotent_and_does_not_rewrite_ownership(business_sessions):
    with business_sessions.begin() as session:
        session.add(db.Run(id="owner", root_id="owner", checkpoint_id="owner", task="x", budgets={}))
        session.flush()
        row = db.RunArtifact(run_id="owner", kind="tool_result", content_hash="a" * 64, content="original")
        session.add(row)
        session.flush()
        identity = row.id
        for _ in range(2):
            replace_references(session, "message", "elsewhere", {"artifact_id": identity})
        assert row.run_id == "owner"
    with business_sessions() as session:
        assert len(list(session.scalars(select(db.ArtifactRetentionReference)))) == 1
