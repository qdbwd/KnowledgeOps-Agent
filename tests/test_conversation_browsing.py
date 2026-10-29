"""APP-03浏览和提交回执合同：真实数据库，禁止截断模型历史或重复接单。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from test_api_worker import client as _client
from test_api_worker import services as _services

from knowledgeops_agent.persistence import models as db

services, client = _services, _client


def chat(client, title="新对话", content="用户正文"):
    row = client.post("/api/conversations", json={"title": title}).json()
    sent = client.post(f"/api/conversations/{row['id']}/messages", json={
        "content": content, "request_key": db.new_id(),
    })
    assert sent.status_code == 202
    return row, sent.json()


def test_all_conversations_are_reachable_and_only_chat_reorders(client):
    chats = [chat(client, f"会话{index}")[0] for index in range(15)]
    client.post("/api/conversations", json={"title": "材料准备"})
    first = client.get("/api/conversations/page?limit=12")
    assert first.status_code == 200
    first = first.json()
    second = client.get("/api/conversations/page", params={"limit": 12, "cursor": first["next_cursor"]}).json()
    assert {r["id"] for r in first["items"] + second["items"]} == {r["id"] for r in chats}
    assert first["items"][0]["id"] == chats[-1]["id"]
    client.patch(f"/api/conversations/{chats[0]['id']}", json={"title": "重命名不置顶"})
    assert client.get("/api/conversations/page").json()["items"][0]["id"] == chats[-1]["id"]
    client.post(f"/api/conversations/{chats[0]['id']}/messages", json={"content": "继续讨论", "request_key": db.new_id()})
    stale = client.get("/api/conversations/page", params={"limit": 12, "cursor": first["next_cursor"]})
    assert stale.status_code == 409 and stale.json()["code"] == "cursor_stale"
    assert client.get("/api/conversations/page").json()["items"][0]["id"] == chats[0]["id"]


def test_search_uses_literal_visible_text_and_excludes_protocol(client, services):
    title, _ = chat(client, "标题独有词", "第一份正文")
    user, run = chat(client, "另一篇", "关键字100%_literal\\保持")
    with services.sessions.begin() as session:
        session.add(db.ChatMessage(conversation_id=user["id"], run_id=run["id"], role="assistant",
            content="AI回答中的独有词", protocol_payload={"reasoning": "秘密协议词"}))
    def search(term):
        response = client.get("/api/conversations/page", params={"q": term})
        assert response.status_code == 200
        return response.json()["items"]
    assert search("标题独有词")[0]["id"] == title["id"]
    assert search("100%_literal\\")[0]["id"] == user["id"]
    match = search("AI回答中的独有词")[0]
    assert match["match_message_id"] and "AI回答" in match["excerpt"]
    assert search("秘密协议词") == []
    assert search("100anything_literal") == []
    with services.sessions.begin() as session:
        session.get(db.Conversation, user["id"]).deletion_state = "deleting"
    assert search("AI回答中的独有词") == []


def test_message_windows_keep_order_and_support_anchors(client, services):
    row, run = chat(client)
    base = f"/api/conversations/{row['id']}/messages"
    with services.sessions.begin() as session:
        for index in range(85):
            session.add(db.ChatMessage(conversation_id=row["id"], run_id=run["id"], role="assistant",
                content=f"保留完整正文{index}", user_sequence=index + 2,
                created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index)))
    full = client.get(base).json()
    latest = client.get(base + "/page")
    assert latest.status_code == 200
    latest = latest.json()
    assert [m["id"] for m in latest["items"]] == [m["id"] for m in full[-40:]]
    assert latest["newer_cursor"] is None
    accumulated = latest["items"]
    cursor = latest["older_cursor"]
    while cursor:
        page = client.get(base + "/page", params={"cursor": cursor}).json()
        accumulated = page["items"] + accumulated
        cursor = page["older_cursor"]
    assert [m["id"] for m in accumulated] == [m["id"] for m in full]
    anchor = client.get(base + "/page", params={"anchor": full[30]["id"]}).json()
    assert full[30]["id"] in {m["id"] for m in anchor["items"]}
    assert anchor["older_cursor"] and anchor["newer_cursor"]
    newer = client.get(base + "/page", params={"cursor": anchor["newer_cursor"]}).json()
    assert not ({m["id"] for m in anchor["items"]} & {m["id"] for m in newer["items"]})
    assert client.get(base + "/page", params={"anchor": db.new_id()}).status_code == 404
    assert len(client.get(base).json()) == 86  # 兼容的完整读取仍供模型上下文使用。


def test_claim_and_guidance_invalidate_message_cursor_without_reordering_activity(client, services):
    row, run = chat(client)
    client.post(f"/api/conversations/{row['id']}/messages", json={"content": "排队消息", "request_key": db.new_id()})
    base = f"/api/conversations/{row['id']}/messages/page"
    before = client.get(base, params={"limit": 1}).json()
    activity = client.get("/api/conversations/page").json()["items"][0]["activity_at"]
    assert services.jobs.claim("worker", run_id=run["id"], generation=run["generation"])
    stale = client.get(base, params={"limit": 1, "cursor": before["older_cursor"]})
    assert stale.status_code == 409 and stale.json()["code"] == "cursor_stale"
    assert client.get("/api/conversations/page").json()["items"][0]["activity_at"] == activity
    prior = client.get(base, params={"limit": 1}).json()
    from knowledgeops_agent.app.runs.guidance import request_guidance
    with services.sessions() as session:
        queued = session.scalar(select(db.Run).where(db.Run.id != run["id"]))
        queued_id = queued.id
    request_guidance(services.jobs, queued_id, target_id=run["id"], target_generation=1)
    assert client.get(base, params={"limit": 1, "cursor": prior["older_cursor"]}).status_code == 409


def test_creation_receipt_survives_rename_and_rejects_changed_request(client, services):
    key = db.new_id()
    body = {"title": "原始名称", "request_key": key}
    first = client.post("/api/conversations", json=body)
    assert first.status_code == 201
    identifier = first.json()["id"]
    client.patch(f"/api/conversations/{identifier}", json={"title": "改过的名称"})
    assert client.post("/api/conversations", json=body).json()["id"] == identifier
    assert client.post("/api/conversations", json={**body, "title": "不同请求"}).status_code == 409
    receipt = client.get("/api/conversations/creation-receipt", params={"request_key": key}).json()
    assert receipt["state"] == "accepted" and receipt["conversation_id"] == identifier
    assert client.get("/api/conversations/creation-receipt", params={"request_key": db.new_id()}).json()["state"] == "not_accepted"
    with services.sessions.begin() as session:
        session.get(db.Conversation, identifier).deletion_state = "deleted"
    assert client.get("/api/conversations/creation-receipt", params={"request_key": key}).json()["state"] == "deleted"
    assert client.post("/api/conversations", json=body).status_code == 404


def test_accepted_submission_is_checked_before_changed_materials_or_settings(client, services, monkeypatch):
    row = client.post("/api/conversations", json={}).json()
    base = f"/api/conversations/{row['id']}"
    source = client.post("/api/sources/text", json={"title": "材料", "content": "保持原文"}).json()
    body = {"content": "同一请求", "request_key": db.new_id(), "source_ids": [source["id"]]}
    accepted = client.post(base + "/messages", json=body)
    assert accepted.status_code == 202
    def unavailable(*args, **kwargs):
        raise ValueError("后来材料或设置已经变化")
    monkeypatch.setattr(services.sources, "get_source", unavailable)
    retry = client.post(base + "/messages", json=body)
    assert retry.status_code == 202 and retry.json()["id"] == accepted.json()["id"]
    assert client.post(base + "/messages", json={**body, "content": "不同内容"}).status_code == 409
    receipt = client.get(base + "/submission-receipt", params={"request_key": body["request_key"]}).json()
    assert receipt["state"] == "accepted" and receipt["run_id"] == accepted.json()["id"]
    assert len(client.get(base + "/messages").json()) == 1


def test_answer_updates_activity_and_invalidates_window(client, services):
    row, run = chat(client)
    chat(client, "后来创建的会话")
    base = f"/api/conversations/{row['id']}/messages/page"
    services.jobs.claim("answer-worker", run_id=run["id"], generation=run["generation"])
    before = client.get(base).json()
    services.jobs.finish(run["id"], "answer-worker", outcome={"answer": "明确的最终回答"})
    after = client.get(base).json()
    assert after["revision"] > before["revision"]
    assert after["items"][-1]["content"] == "明确的最终回答"
    assert client.get("/api/conversations/page").json()["items"][0]["id"] == row["id"]


def test_concurrent_retries_create_only_one_conversation_and_run(client, services):
    from concurrent.futures import ThreadPoolExecutor

    creation = {"title": "同一份新草稿", "request_key": db.new_id()}
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda _: client.post("/api/conversations", json=creation), range(3)))
    assert all(response.status_code == 201 for response in responses)
    identities = {response.json()["id"] for response in responses}
    assert len(identities) == 1
    conversation_id = identities.pop()
    payload = {"content": "同一条提交", "request_key": db.new_id()}
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda _: client.post(f"/api/conversations/{conversation_id}/messages", json=payload), range(3)))
    assert all(response.status_code == 202 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1
    assert len(client.get(f"/api/conversations/{conversation_id}/messages").json()) == 1


def test_cursor_scope_validation_and_namespace(client):
    first, _ = chat(client)
    second, _ = chat(client)
    directory = client.get("/api/conversations/page?limit=1").json()
    assert client.get("/api/conversations/page", params={"limit": 1, "q": "另一个查询", "cursor": directory["next_cursor"]}).status_code == 409
    assert client.get(f"/api/conversations/{first['id']}/messages/page", params={"cursor": "not-valid-json"}).status_code == 409
    foreign = client.get(f"/api/conversations/{second['id']}/messages").json()[0]["id"]
    assert client.get(f"/api/conversations/{first['id']}/messages/page", params={"anchor": foreign}).status_code == 404
    assert client.get("/api/conversations/page?limit=101").status_code == 422
    state = client.get("/api/client-state").json()
    assert state["contract_version"] == 1
    assert state["namespace"] == client.get("/api/client-state").json()["namespace"]
    assert "postgres" not in state["namespace"] and "://" not in state["namespace"]


def test_legacy_and_expired_receipts_do_not_refreeze_or_reaccept(client, services):
    from types import SimpleNamespace

    from knowledgeops_agent.app.lifecycle.idempotency import freeze_request

    row = services.jobs.create_conversation()
    key = db.new_id()
    accepted = services.jobs.enqueue(row["id"], "旧客户端请求", request_key=key)
    def unavailable(*args, **kwargs):
        raise ValueError("现在已没有旧档位")
    services.reasoning = SimpleNamespace(freeze=unavailable)
    base = f"/api/conversations/{row['id']}/messages"
    payload = {"content": "旧客户端请求", "request_key": key}
    assert client.post(base, json=payload).json()["id"] == accepted["id"]
    with services.sessions.begin() as session:
        run = session.get(db.Run, accepted["id"])
        freeze_request(run)
        run.lifecycle_state, run.task, run.input_data = "expired", "", {}
    retry = client.post(base, json=payload)
    assert retry.status_code == 202 and retry.json()["id"] == accepted["id"]
    assert client.post(base, json={**payload, "auxiliary": True}).status_code == 409
