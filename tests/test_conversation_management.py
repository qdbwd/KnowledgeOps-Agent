"""会话目录仅显示真实聊天；重命名不改消息，删除边界仍适用。"""

from test_api_worker import client as _client
from test_api_worker import services as _services

from knowledgeops_agent.persistence import models as db

services, client = _services, _client


def test_empty_attachment_conversation_is_hidden_until_first_message(client, services):
    chat = client.post("/api/conversations", json={}).json()
    base = f"/api/conversations/{chat['id']}"
    assert client.get("/api/conversations").json() == []
    source = client.post("/api/sources/text", json={
        "title": "未发送材料", "content": "等待用户提问的正文。", "conversation_id": chat["id"],
    })
    assert source.status_code == 201
    assert client.get("/api/conversations").json() == []
    imported = services.jobs.enqueue_material({"type": "web", "url": "https://example.com", "conversation_id": chat["id"]}, request_key=db.new_id())
    with services.sessions.begin() as session:
        session.add(db.ChatMessage(conversation_id=chat["id"], run_id=imported["id"], role="assistant", content="材料导入回执"))
    assert client.get("/api/conversations").json() == []
    message = client.post(base + "/messages", json={"content": "分析这份材料", "request_key": db.new_id()})
    assert message.status_code == 202
    assert client.get("/api/conversations").json()[0]["title"] == "分析这份材料"


def test_rename_is_persistent_and_does_not_modify_messages(client, services):
    chat = client.post("/api/conversations", json={"title": "原名称"}).json()
    base = f"/api/conversations/{chat['id']}"
    client.post(base + "/messages", json={"content": "保持消息原文", "request_key": db.new_id()})
    before = client.get(base + "/messages").json()
    renamed = client.patch(base, json={"title": "  新名称  "})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "新名称"
    assert client.get("/api/conversations").json()[0]["title"] == "新名称"
    assert client.get(base + "/messages").json() == before
    assert client.patch(base, json={"title": "  "}).status_code == 422
    assert client.patch(base, json={"title": "长" * 301}).status_code == 422
    with services.sessions.begin() as session:
        session.get(db.Conversation, chat["id"]).deletion_state = "deleting"
    assert client.patch(base, json={"title": "不能复活"}).status_code == 404
    assert client.get("/api/conversations").json() == []


def test_review_pending_filter_includes_drafts_and_approved_only(client, services):
    with services.sessions.begin() as session:
        for status in ("draft", "approved", "published", "rejected"):
            session.add(db.ChangeSet(root_id="review-filter-" + status, status=status, operations=[]))
    response = client.get("/api/changesets/page?status=pending")
    assert response.status_code == 200
    assert {row["status"] for row in response.json()["items"]} == {"draft", "approved"}
