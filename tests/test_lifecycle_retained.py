"""材料目录不等于正文引用；快照祖先和历史副本可以在证明不可达后释放。"""

import hashlib

from langchain_core.messages import HumanMessage
from sqlalchemy import select

from knowledgeops_agent.app.lifecycle.references import replace_references
from knowledgeops_agent.app.lifecycle.retained import (
    history_reachability,
    pin_checkpoint_artifacts,
    prune_history,
    prune_snapshots,
)
from knowledgeops_agent.harness.context.history import _store_records, visible_records
from knowledgeops_agent.harness.runtime.resources import content_hash
from knowledgeops_agent.persistence import models as db


def run_row(session, conversation, *, state="active"):
    identity = db.new_id()
    run = db.Run(id=identity, root_id=identity, conversation_id=conversation,
                 checkpoint_id=identity, task="fixture", budgets={}, status="completed",
                 lifecycle_state=state, finished_at=db.utcnow())
    session.add(run)
    session.flush()
    return run


def snapshot_row(session, chat, run, *, content, parent=None):
    snapshot = db.ContinuationSnapshot(
        conversation_id=chat.id, run_id=run.id, generation=1, covered_sequence=1,
        content=content, content_hash=content_hash(content), parent_snapshot_id=parent,
    )
    session.add(snapshot)
    session.flush()
    replace_references(session, "snapshot", snapshot.id, content, snapshot=True)
    return snapshot


def test_old_snapshot_is_pinned_by_live_run_then_released_without_changing_latest_body(business_sessions):
    sessions = business_sessions
    with sessions.begin() as session:
        chat = db.Conversation()
        session.add(chat)
        session.flush()
        old = run_row(session, chat.id, state="cleaning")
        artifact = db.RunArtifact(run_id=old.id, kind="tool_result", content="old", content_hash="a" * 64)
        session.add(artifact)
        session.flush()
        first = snapshot_row(session, chat, old, content={"task_notes": {"artifact_id": artifact.id}})
        recent = run_row(session, chat.id)
        recent.continuation_snapshot_id = first.id
        latest_content = {"artifact_ids": [artifact.id], "summary": "新摘要"}
        latest = snapshot_row(session, chat, recent, content=latest_content, parent=first.id)
        chat.latest_snapshot_id = latest.id
        session.flush()
        assert prune_snapshots(session, chat.id)["deleted"] == 0
        recent.lifecycle_state = "cleaning"
        session.flush()
        assert prune_snapshots(session, chat.id)["deleted"] == 1
        assert session.get(db.ContinuationSnapshot, first.id) is None
        session.refresh(latest)
        assert latest.parent_snapshot_id is None
        assert latest.content == latest_content and latest.content_hash == content_hash(latest_content)
        assert session.get(db.Run, recent.id).continuation_snapshot_id is None
        assert list(session.scalars(select(db.ArtifactRetentionReference))) == []


def test_history_keeps_snapshot_batches_and_working_messages_but_removes_unreachable_rows(business_sessions):
    sessions = business_sessions
    scope = hashlib.sha256(b"scope").hexdigest()
    messages = [HumanMessage(value, id=value) for value in ("old", "current", "live")]
    records = visible_records(messages)
    with sessions.begin() as session:
        chat = db.Conversation()
        session.add(chat)
        session.flush()
        old = run_row(session, chat.id, state="cleaning")
        _store_records(session, scope, records)
        for name, source in (("old-batch", records[0]), ("kept-batch", records[1])):
            session.add(db.TaskHistoryBatch(scope=scope, id=name, run_id=old.id, source_ids=[source["id"]]))
        latest = snapshot_row(session, chat, old, content={
            "task_history": {"scope": scope, "batches": ["kept-batch"]},
        })
        chat.latest_snapshot_id = latest.id
        session.flush()
        batches, sources = history_reachability(session, scope, states=[{"messages": [messages[2]]}])
        result = prune_history(session, scope, protected_batches=batches, protected_sources=sources, limit=1)
        assert result["batches"] == 1 and result["remaining"]
        result = prune_history(session, scope, protected_batches=batches, protected_sources=sources, limit=1)
        assert result["records"] == 1 and not result["remaining"]
        assert session.get(db.TaskHistoryRecord, (scope, records[0]["id"])) is None
        assert session.get(db.TaskHistoryRecord, (scope, records[1]["id"]))
        assert session.get(db.TaskHistoryRecord, (scope, records[2]["id"]))
        assert not list(session.scalars(select(db.TaskHistoryChunk).where(db.TaskHistoryChunk.source_id == records[0]["id"])))


def test_live_owner_batch_is_protected_even_before_snapshot_is_committed(business_sessions):
    with business_sessions.begin() as session:
        run = run_row(session, None)
        record = visible_records([HumanMessage("尚未提交快照的原话")])[0]
        _store_records(session, "scope", [record])
        session.add(db.TaskHistoryBatch(scope="scope", id="live", run_id=run.id, source_ids=[record["id"]]))
        session.flush()
        assert prune_history(session, "scope", protected_batches=set(), protected_sources=set()) == {
            "batches": 0, "records": 0, "remaining": False,
        }
        assert session.get(db.TaskHistoryRecord, ("scope", record["id"])) is not None


def test_checkpoint_reference_is_replaceable_and_does_not_grant_access(business_sessions):
    with business_sessions.begin() as session:
        owner = run_row(session, None)
        consumer = run_row(session, None)
        artifact = db.RunArtifact(run_id=owner.id, kind="tool_result", content="text", content_hash="a" * 64)
        session.add(artifact)
        session.flush()
        pin_checkpoint_artifacts(session, consumer.id, {"task_notes": {"artifact_id": artifact.id}})
        assert len(list(session.scalars(select(db.ArtifactRetentionReference)))) == 1
        pin_checkpoint_artifacts(session, consumer.id, {})
        assert list(session.scalars(select(db.ArtifactRetentionReference))) == []
        assert consumer.continuation_snapshot_id is None  # 保留引用没有给其他根新增读取许可。
