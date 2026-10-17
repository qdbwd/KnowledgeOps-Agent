"""清理状态是服务边界，不能仅靠前端隐藏按钮阻止旧调用和旧页面读取。"""

import asyncio

import pytest
from sqlalchemy import select
from test_jobs import jobs as _jobs

from knowledgeops_agent.app.runs.events import iter_run_events
from knowledgeops_agent.persistence import models as db

jobs = _jobs


def test_expired_details_cannot_resume_append_or_recreate_artifacts(jobs):
    _, _, service, sessions, clock = jobs
    conversation = service.create_conversation()["id"]
    run = service.enqueue(conversation, "完成后保留聊天", request_key="expiry")
    service.claim("w", run_id=run["id"], generation=1)
    artifact = service.save_artifact(run["id"], "tool_result", "original")
    service.finish(run["id"], "w", status="failed", outcome={"answer": "部分结果"})
    with sessions.begin() as session:
        row = session.get(db.Run, run["id"])
        row.lifecycle_state = "expired"
        row.details_summary = {"status": "failed", "answer": "部分结果"}
        session.get(db.RunArtifact, artifact).expired_at = clock[0]
    assert service.get_run(run["id"])["details_available"] is False
    assert service.get_run(run["id"])["resumable"] is False
    for operation in (
        lambda: service.resume(run["id"]),
        lambda: service.append_event(run["id"], "late", {}),
        lambda: service.save_artifact(run["id"], "tool_result", "late write"),
        lambda: service.read_artifact(artifact, root_id=run["id"]),
    ):
        with pytest.raises(ValueError, match="expired|清理|过期"):
            operation()
    assert service.events(run["id"]) == []
    assert service.messages(conversation)[-1]["content"] == "部分结果"
    assert service.artifacts(run["id"])[0]["available"] is False
    assert service.claim("other", run_id=run["id"], generation=1) is None
    assert service.enqueue(conversation, "继续新任务", request_key="new")["status"] == "queued"


def test_deleted_conversation_is_hidden_and_cannot_be_read_or_written(jobs):
    _, _, service, sessions, _ = jobs
    conversation = service.create_conversation()["id"]
    run = service.enqueue(conversation, "待清理内容", request_key="deleted")
    with sessions.begin() as session:
        session.get(db.Conversation, conversation).deletion_state = "deleting"
    assert service.conversations() == []
    assert service.list_runs() == []
    for operation in (
        lambda: service.messages(conversation),
        lambda: service.get_run(run["id"]),
        lambda: service.events(run["id"]),
        lambda: service.enqueue(conversation, "待清理内容", request_key="deleted"),
        lambda: service.enqueue(conversation, "new", request_key="new"),
    ):
        with pytest.raises(KeyError):
            operation()
    assert service.claim("late-worker", run_id=run["id"], generation=1) is None
    with sessions() as session:
        assert len(list(session.scalars(select(db.ChatMessage)))) == 1


def test_expired_sse_closes_without_replaying_stale_events(jobs):
    _, _, service, sessions, _ = jobs
    chat = service.create_conversation()["id"]
    run = service.enqueue(chat, "old", request_key="stream")
    with sessions.begin() as session:
        row = session.get(db.Run, run["id"])
        row.lifecycle_state, row.status = "expired", "completed"
        # 保留一个旧序号模拟已经删除物理事件；不能无限等待序号补齐。
        row.event_sequence = 200

    async def collect():
        async def disconnected():
            return False
        return [item async for item in iter_run_events(service, None, run["id"], 0, disconnected)]

    result = asyncio.run(asyncio.wait_for(collect(), timeout=3))
    assert len(result) == 1 and "event: closed" in result[0]
    assert "details_expired" in result[0]


def test_deleted_conversation_closes_an_existing_stream(jobs):
    _, _, service, sessions, _ = jobs
    chat = service.create_conversation()["id"]
    run = service.enqueue(chat, "旧内容", request_key="stream-delete")
    with sessions.begin() as session:
        session.get(db.Conversation, chat).deletion_state = "deleting"

    async def collect():
        async def disconnected():
            return False
        return [item async for item in iter_run_events(service, None, run["id"], 0, disconnected)]

    result = asyncio.run(asyncio.wait_for(collect(), timeout=3))
    assert len(result) == 1 and "conversation_deleted" in result[0]
    assert "旧内容" not in result[0]
