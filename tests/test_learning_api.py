"""学习配置HTTP与资源装配使用真实服务；无供应商调用。"""

import pytest
from test_api_worker import client as _client
from test_api_worker import services as _services

from knowledgeops_agent.file_store import FileStore

services = _services
client = _client


@pytest.fixture(autouse=True)
def profile_workspace(services, tmp_path):
    services.sessions.configure(info={"file_store": FileStore(tmp_path / "profiles-test")})


def test_http_task_profile_import_and_chat_link(client, services):
    conversation = client.post("/api/conversations", json={"title": "配置学习"}).json()
    task = client.post(
        "/api/learning-tasks",
        json={
            "conversation_id": conversation["id"],
            "request_key": "task-create",
            "title": "事务隔离",
            "goal": "理解隔离级别",
            "topic": "数据库",
        },
    )
    assert task.status_code == 201, task.text
    task_id = task.json()["id"]
    profile = client.get("/api/learning-profiles", params={"task_id": task_id}).json()
    assert profile["task_id"] == task_id
    imported = client.post(
        f"/api/learning-profiles/{profile['id']}/import-preview",
        json={"expected_revision": 1, "filename": "reader.md", "content": "用SQL执行序列解释。"},
    )
    assert imported.status_code == 200
    preview = imported.json()
    applied = client.post(
        f"/api/learning-profiles/{profile['id']}/import-apply",
        json={"preview_id": preview["id"], "expected_hash": preview["sha256"]},
    )
    assert applied.json()["content"] == "用SQL执行序列解释。"
    assert (
        client.get(f"/api/learning-profiles/{profile['id']}/export").text == "用SQL执行序列解释。"
    )
    conflict = client.patch(
        f"/api/learning-profiles/{profile['id']}",
        json={"expected_revision": 1, "content": "过期编辑"},
    )
    assert conflict.status_code == 409
    run = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "先解释脏读", "request_key": "new-user-message"},
    )
    assert run.status_code == 202
    assert run.json()["learning_task_id"] == task_id and run.json()["origin"] == "human"
    assert len(services.jobs.messages(conversation["id"])) == 1
    assert services.gateway.ledger.records() == []


def test_task_cannot_attach_to_another_conversation(client):
    first = client.post("/api/conversations", json={"title": "一"}).json()
    second = client.post("/api/conversations", json={"title": "二"}).json()
    task = client.post(
        "/api/learning-tasks",
        json={
            "conversation_id": first["id"],
            "request_key": "one",
            "title": "事务",
            "goal": "学事务",
        },
    ).json()
    rejected = client.post(
        f"/api/conversations/{second['id']}/messages",
        json={"content": "继续", "request_key": "wrong-task", "learning_task_id": task["id"]},
    )
    assert rejected.status_code == 409
    assert client.get(f"/api/conversations/{second['id']}/messages").json() == []


def test_task_requirements_reuse_and_rollback_never_copy_authority(client):
    conv = client.post("/api/conversations", json={"title": "复用配置"}).json()

    def create(key):
        return client.post(
            "/api/learning-tasks",
            json={
                "conversation_id": conv["id"],
                "request_key": key,
                "title": key,
                "goal": "学习事务",
            },
        ).json()

    source, target = create("source"), create("target")
    updated = client.post(
        f"/api/learning-tasks/{source['id']}/configuration",
        json={"expected_revision": 1, "changes": {"examples": "先给SQL例子"}},
    ).json()
    copied = client.post(
        f"/api/learning-tasks/{target['id']}/configuration-reuse",
        json={
            "expected_revision": 1,
            "source_task_id": source["id"],
            "source_revision": updated["revision"],
        },
    )
    assert copied.status_code == 200, copied.text
    value = copied.json()
    assert value["configuration"]["examples"]["value"] == "先给SQL例子"
    assert value["configuration"]["examples"]["source"]["reused_task_id"] == source["id"]
    assert not value["auto_publish"] and value["confirmed_requirements_revision"] is None
    assert value["status"] == "draft"
    assert (
        client.post(
            f"/api/learning-tasks/{target['id']}/configuration-reuse",
            json={"expected_revision": 1, "source_task_id": source["id"], "source_revision": 1},
        ).status_code
        == 409
    )
    reverted = client.post(
        f"/api/learning-tasks/{target['id']}/configuration-reuse",
        json={
            "expected_revision": value["revision"],
            "source_task_id": target["id"],
            "source_revision": 1,
        },
    )
    assert reverted.status_code == 200 and reverted.json()["configuration"] == {}
