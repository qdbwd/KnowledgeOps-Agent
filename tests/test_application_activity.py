"""APP-02 活动聚合与独立已读回执；运行结果仍由既有业务服务产生。"""

from sqlalchemy import select
from test_api_worker import client as _client
from test_api_worker import services as _services

from knowledgeops_agent.persistence import models as db

services, client = _services, _client


def queued(client, title="活动验收"):
    chat = client.post("/api/conversations", json={"title": title}).json()
    reply = client.post(f"/api/conversations/{chat['id']}/messages", json={
        "content": title, "request_key": db.new_id(),
    })
    assert reply.status_code == 202
    return chat, reply.json()


def finish(jobs, run, *, status="completed", outcome=None):
    assert jobs.claim("activity-worker", run_id=run["id"], generation=run["generation"])
    return jobs.finish(run["id"], "activity-worker", status=status, outcome=outcome or {"answer": "已完成的公开结果"})


def test_root_results_are_unread_and_late_read_cannot_clear_newer_version(client, services):
    chat, run = queued(client)
    first = finish(services.jobs, run, status="failed", outcome={"answer": "保留第一版进展"})
    feed = client.get("/api/activity", params={"conversation_ids": chat["id"]})
    assert feed.status_code == 200
    feed = feed.json()
    first_clock = feed["revision"]
    assert feed["counts"]["attention"] == 1 and feed["counts"]["unread"] == 1
    shown = client.get("/api/activity/runs?view=unread").json()["items"][0]
    assert shown["id"] == run["id"] and shown["result_version"] > 0
    old_version = shown["result_version"]
    ack = client.post(f"/api/activity/runs/{run['id']}/read", json={"version": old_version})
    assert ack.status_code == 200 and ack.json()["unread"] is False
    read_clock = client.get("/api/activity").json()["revision"]
    assert read_clock > first_clock
    client.post(f"/api/activity/runs/{run['id']}/read", json={"version": old_version})
    assert client.get("/api/activity").json()["revision"] == read_clock
    assert services.jobs.get_run(run["id"])["status"] == first["status"]
    assert client.get("/api/activity").json()["counts"]["attention"] == 1
    resumed = services.jobs.resume(run["id"])
    finish(services.jobs, resumed)
    stale = client.post(f"/api/activity/runs/{run['id']}/read", json={"version": old_version})
    assert stale.status_code == 200 and stale.json()["unread"] is True
    latest = client.get("/api/activity").json()
    assert latest["counts"]["attention"] == 0 and latest["counts"]["unread"] == 1
    assert client.post(f"/api/activity/runs/{run['id']}/read", json={"version": 999999}).status_code == 409


def test_all_kinds_are_visible_children_aggregate_and_read_does_not_answer(client, services):
    chat, root = queued(client)
    assert services.jobs.claim("activity-worker", run_id=root["id"], generation=root["generation"])
    child = services.jobs.start_child(root["id"], "子任务公开问题")
    finish(services.jobs, child, status="interrupted", outcome={"kind": "waiting_approval"})
    for kind in ("material", "context_compaction", "memory_classification"):
        _, other = queued(client, kind)
        with services.sessions.begin() as session:
            session.get(db.Run, other["id"]).kind = kind
    rows = client.get("/api/activity/runs?view=all").json()["items"]
    assert len(rows) == 4 and child["id"] not in {item["id"] for item in rows}
    assert {item["kind"] for item in rows} == {"agent", "material", "context_compaction", "memory_classification"}
    group = next(item for item in rows if item["id"] == root["id"])
    assert group["attention"] and group["children"][0]["id"] == child["id"]
    assert "approval" in group["attention_reasons"]
    before = services.jobs.get_run(child["id"])
    assert client.post(f"/api/activity/runs/{root['id']}/read", json={"version": group["result_version"]}).status_code == 200
    after = services.jobs.get_run(child["id"])
    assert (after["status"], after["generation"], after["outcome"]) == (before["status"], before["generation"], before["outcome"])
    assert client.get("/api/activity", params={"conversation_ids": chat["id"]}).json()["conversations"][chat["id"]]["attention"]


def test_deleted_conversations_and_removed_runs_disappear_without_reads_retaining_content(client, services):
    chat, removed = queued(client, "会话被删除")
    _, hidden = queued(client, "运行被移除")
    finish(services.jobs, removed)
    with services.sessions.begin() as session:
        session.get(db.Conversation, chat["id"]).deletion_state = "deleting"
        session.get(db.Run, hidden["id"]).user_deleted_at = db.utcnow()
    assert client.get("/api/activity").json()["counts"]["all"] == 0
    assert client.get("/api/activity/runs?view=all").json()["items"] == []
    assert client.post(f"/api/activity/runs/{removed['id']}/read", json={"version": 1}).status_code == 404
    with services.sessions() as session:
        assert not list(session.scalars(select(db.ArtifactRetentionReference).where(db.ArtifactRetentionReference.owner_kind == "activity")))


def test_management_operations_link_original_pages_and_do_not_copy_sensitive_details(client, services):
    with services.sessions.begin() as session:
        session.add(db.ExtensionOperation(kind="mcp_candidate", target="local-tool", status="failed",
            parameters={"credential": "must-not-leak"}, result={"log": "private log"}, error_code="prepare_failed"))
        session.add(db.CleanupJob(target_kind="source", target_id="old-source", status="failed", last_error="secret path"))
    reply = client.get("/api/activity")
    assert reply.status_code == 200
    notices = reply.json()["management"]["items"]
    assert {item["href"].split("?")[0] for item in notices} == {"/mcp", "/sources"}
    assert reply.json()["management"]["attention"] == 2
    assert "must-not-leak" not in reply.text and "private log" not in reply.text and "secret path" not in reply.text


def test_older_child_attention_is_not_lost_when_only_three_summaries_are_shown(client, services):
    _, root = queued(client)
    with services.sessions.begin() as session:
        # 创建历史子运行，不改变实际调度上限；只有最早的子运行仍等待核对。
        for index in range(5):
            identity = db.new_id()
            session.add(db.Run(id=identity, root_id=root["id"], parent_id=root["id"], checkpoint_id=identity,
                task=f"历史子任务{index}", budgets={},
                status="interrupted" if index == 0 else "completed",
                outcome={"kind": "execution_unknown"} if index == 0 else {},
                created_at=db.utcnow()))
    row = client.get("/api/activity/runs").json()["items"][0]
    assert row["attention"] and row["child_count"] == 5 and len(row["children"]) == 3
    assert row["attention_reasons"] == ["unknown"]


def test_management_overflow_is_reachable_and_cleanup_links_target_the_existing_handler(client, services):
    with services.sessions.begin() as session:
        for index in range(23):
            session.add(db.CleanupJob(target_kind="conversation", target_id=db.new_id(), status="failed",
                summary={"title": f"删除历史{index}"}, last_error="must-not-leak"))
        cleanup = db.ExtensionOperation(kind="mcp_cleanup", target="old-tool", status="waiting")
        session.add(cleanup)
        session.flush()
        cleanup_id = cleanup.id
    feed = client.get("/api/activity").json()["management"]
    assert feed["truncated"] and feed["attention"] == 23
    assert any(item["href"] == f"/mcp?cleanup={cleanup_id}" for item in feed["items"])
    found, cursor = [], None
    while True:
        response = client.get("/api/activity/management", params={"kind": "deletion", "limit": 7, **({"cursor": cursor} if cursor else {})})
        assert response.status_code == 200
        assert "must-not-leak" not in response.text
        page = response.json()
        found.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len({item["id"] for item in found}) == len(found) == 23
    assert all(item["href"] == f"/conversations?deletion={item['target_id']}" for item in found)
