"""游标分页按数据库过滤，插入新记录后也不挤掉下一页的旧条目。"""

from datetime import UTC, datetime, timedelta

from knowledgeops_agent.db import ChangeSet, Run
from knowledgeops_agent.jobs import RunService
from knowledgeops_agent.knowledge import KnowledgeService


def seed(sessions):
    jobs = RunService(sessions)
    conversation = jobs.create_conversation()
    root = jobs.enqueue(conversation["id"], "旧会话", request_key="original")
    other = jobs.create_conversation()
    newer = jobs.enqueue(other["id"], "其他会话", request_key="newer")
    base = datetime(2025, 1, 1, tzinfo=UTC)
    with sessions.begin() as session:
        for index in range(105):
            session.add(
                ChangeSet(
                    root_id=root["id"] if index < 4 else newer["id"],
                    operations=[],
                    created_at=base + timedelta(seconds=index),
                )
            )
    return jobs, conversation, root


def test_draft_filter_precedes_pagination(business_sessions):
    jobs, conversation, root = seed(business_sessions)
    knowledge = KnowledgeService(business_sessions)
    first = knowledge.drafts_page(conversation_id=conversation["id"], limit=2)
    assert len(first["items"]) == 2 and first["next_cursor"]
    assert all(draft["root_id"] == root["id"] for draft in first["items"])
    second = knowledge.drafts_page(
        conversation_id=conversation["id"], limit=2, cursor=first["next_cursor"]
    )
    assert len(second["items"]) == 2 and second["next_cursor"] is None
    assert not ({x["id"] for x in first["items"]} & {x["id"] for x in second["items"]})


def test_new_draft_insert_does_not_shift_existing_cursor(business_sessions):
    _, _, root = seed(business_sessions)
    knowledge = KnowledgeService(business_sessions)
    first = knowledge.drafts_page(limit=100)
    with business_sessions.begin() as session:
        session.add(ChangeSet(root_id=root["id"], operations=[]))
    second = knowledge.drafts_page(cursor=first["next_cursor"], limit=100)
    assert len(first["items"]) == 100 and len(second["items"]) == 5
    assert len({x["id"] for x in first["items"] + second["items"]}) == 105


def test_runs_page_filters_roots_and_running_before_limit(business_sessions):
    jobs = RunService(business_sessions)
    conversation = jobs.create_conversation()
    running = jobs.enqueue(conversation["id"], "正在执行", request_key="active")
    jobs.claim("worker", run_id=running["id"], generation=1)
    jobs.start_child(running["id"], "子任务")
    for index in range(5):
        jobs.enqueue(conversation["id"], f"排队{index}", request_key=str(index))
    active = jobs.runs_page(conversation_id=conversation["id"], status="running", roots_only=True)
    assert [run["id"] for run in active["items"]] == [running["id"]]
    queued = jobs.runs_page(
        conversation_id=conversation["id"], status="queued", roots_only=True, limit=2
    )
    assert len(queued["items"]) == 2 and queued["next_cursor"]
    with business_sessions() as session:
        assert session.get(Run, running["id"]).status == "running"
