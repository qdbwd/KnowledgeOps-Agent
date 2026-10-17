"""HTTP删除预览、接受、状态、重试与已回收作业的完成查询。"""

from datetime import timedelta

from test_api_worker import client as _client
from test_api_worker import services as _services

from knowledgeops_agent.persistence import models as db
from knowledgeops_agent.persistence.files import FileStore

services, client = _services, _client


def test_http_delete_requires_current_preview_and_remains_queryable(client, services, tmp_path):
    services.sessions.configure(info={"file_store": FileStore(tmp_path)})
    chat = client.post("/api/conversations", json={"title": "删除预览"}).json()
    base = "/api/conversations/" + chat["id"]
    preview = client.get(base + "/deletion-preview").json()
    assert preview["eligible"] and "长期记忆正文" in preview["preserved"]
    rejected = client.request("DELETE", base, json={"expected_version": "0" * 64})
    assert rejected.status_code == 409
    accepted = client.request("DELETE", base, json={"expected_version": preview["version"]})
    assert accepted.status_code == 202
    assert not {"claim_token", "claim_until", "cursor"} & accepted.json().keys()
    assert client.get(base + "/messages").status_code == 404
    assert client.get("/api/conversations").json() == []
    assert client.post(base + "/deletion/retry").status_code == 202
    for _ in range(30):
        state = client.get(base + "/deletion").json()
        if state["status"] == "completed":
            break
        assert state["status"] != "failed", state
        services.lifecycle.advance(accepted.json()["id"])
    assert client.get(base + "/deletion").json()["status"] == "completed"
    with services.sessions.begin() as session:
        job = session.get(db.CleanupJob, accepted.json()["id"])
        job.finished_at = db.utcnow() - timedelta(days=31)
    assert services.lifecycle.jobs.prune_completed() == 1
    state = client.get(base + "/deletion").json()
    assert state["status"] == "completed" and state["id"] is None
    assert client.post(base + "/deletion/retry").json()["status"] == "completed"
    replay = client.request("DELETE", base, json={"expected_version": preview["version"]})
    assert replay.status_code == 202 and replay.json()["status"] == "completed"
    with services.sessions() as session:
        assert session.get(db.CleanupJob, accepted.json()["id"]) is None


def test_http_delete_blocks_queued_run(client):
    chat = client.post("/api/conversations", json={}).json()
    base = "/api/conversations/" + chat["id"]
    client.post(base + "/messages", json={"content": "待执行", "request_key": db.new_id()})
    preview = client.get(base + "/deletion-preview").json()
    assert not preview["eligible"]
    response = client.request("DELETE", base, json={"expected_version": preview["version"]})
    assert response.status_code == 409
    assert len(client.get(base + "/messages").json()) == 1
