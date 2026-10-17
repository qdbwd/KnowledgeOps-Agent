"""子任务管理走同一持久服务：接口不扩大权限，重发不重复追加。"""

from sqlalchemy import select
from test_api_worker import client as _client
from test_api_worker import enqueue
from test_api_worker import services as _services

from knowledgeops_agent.app.runs.children import ChildRuns
from knowledgeops_agent.persistence.models import ChildInstruction

client = _client
services = _services


def setup_family(client, services):
    _, root, _ = enqueue(client)
    services.jobs.claim("parent", run_id=root["id"], generation=1)
    first = services.jobs.start_child(root["id"], "核对原理", operation_key="api-first")
    second = services.jobs.start_child(root["id"], "核对实践", operation_key="api-second")
    return root, first, second


def test_child_api_persists_human_instruction_without_resuming_paused_group(client, services):
    root, first, second = setup_family(client, services)
    ChildRuns(services.jobs).wait(root["id"], "parent", [first["id"], second["id"]])
    services.jobs.pause(root["id"])
    path = f"/api/runs/{root['id']}/children/{first['id']}"
    assert client.get(f"/api/runs/{root['id']}").json()["child_count"] == 2
    assert len(client.get(f"/api/runs/{root['id']}/children").json()) == 2
    body = {"request_key": "human-once", "message": "补充前置条件，不要扩大题目范围"}
    response = client.post(path + "/instructions", json=body)
    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert client.post(path + "/instructions", json=body).json() == response.json()
    assert client.post(path + "/instructions", json={**body, "message": "替换已提交内容"}).status_code == 409
    with services.sessions() as session:
        instructions = list(session.scalars(select(ChildInstruction)))
        assert len(instructions) == 1
        assert instructions[0].origin == "human" and instructions[0].status == "pending"
    assert services.jobs.get_run(root["id"])["status"] == "paused"
    assert client.get(path).json()["status"] == "paused"


def test_single_child_cancel_is_scoped_and_keeps_sibling_and_budget(client, services):
    root, first, second = setup_family(client, services)
    _, other, _ = enqueue(client)
    wrong = f"/api/runs/{other['id']}/children/{first['id']}"
    assert client.get(wrong).status_code == 404
    assert client.post(wrong + "/cancel").status_code == 404
    path = f"/api/runs/{root['id']}/children/{first['id']}"
    assert client.post(path + "/cancel").json()["status"] == "cancelled"
    assert services.jobs.get_run(second["id"])["status"] == "queued"
    parent = services.jobs.get_run(root["id"])
    assert parent["status"] == "running" and not parent["cancel_requested"]
    assert parent["budgets"] == root["budgets"]
    assert client.post(path + "/instructions", json={"request_key": "late", "message": "继续做"}).status_code == 409


def test_child_management_rejects_non_root_and_spoofed_instruction_origin(client, services):
    root, first, _ = setup_family(client, services)
    assert client.get(f"/api/runs/{first['id']}/children").status_code == 403
    path = f"/api/runs/{root['id']}/children/{first['id']}/instructions"
    assert client.post(path, json={"request_key": "bad", "message": "继续", "origin": "agent"}).status_code == 422
    assert client.get("/api/runs/missing/children").status_code == 404
