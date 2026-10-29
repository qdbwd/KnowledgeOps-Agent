"""APP-04 分层就绪与任务阻塞原因；只读探测，不领取任务或调用模型。"""

from test_api_worker import client as _client
from test_api_worker import enqueue as _enqueue
from test_api_worker import services as _services

services, client = _services, _client


def ready(client):
    response = client.get("/api/readiness")
    assert response.status_code == 200
    return response.json()


def test_readiness_separates_foundation_from_execution_and_caches_briefly(client, services):
    """API 已起但 worker 未起时，要能区分"基础就绪"与"执行未就绪"。"""
    # 测试进程会继承 .env 的真实回执库地址；这里显式清空，避免单元测试
    # 去连接正式回执库，同时验证"未配置"必须如实降级。
    services.settings.accounting_database_url = ""
    first = ready(client)
    assert first["cached"] is False
    layers = first["layers"]
    assert layers["process"]["ok"] is True
    assert layers["foundation"]["database"]["ok"] is True
    # 单元测试 schema 由 models.create_all 建立，没有 Alembic 版本表：
    # 探测必须如实报告未就绪，不能把"库能连"猜成"结构一致"。
    migration = layers["foundation"]["migration"]
    assert migration["ok"] is False and migration["current"] is None and migration["head"]
    # 测试夹具没有配置回执库，这项必须如实降级，而不是被当成正常。
    assert layers["foundation"]["receipts"]["ok"] is False
    # 没有 worker 进程持有租约：执行层离线，但基础层不应因此变成故障。
    assert layers["execution"]["worker_lease"] is False
    assert layers["execution"]["state"] == "offline"
    assert {"queued_runs", "due_dispatches", "retrying_dispatches"} <= set(layers["backlog"])
    assert first["status"] == "blocked"

    second = ready(client)
    assert second["cached"] is True


def test_blocking_reason_mirrors_existing_claim_rules(client, services):
    """阻塞原因来自现有领取规则：根任务组、同会话未决、worker 未就绪。"""
    _, first, _ = _enqueue(client)
    waiting = client.get(f"/api/readiness/runs/{first['id']}").json()
    assert waiting["reason"] == "worker_offline" and waiting["claimable"] is False

    assert services.jobs.claim("readiness-worker", run_id=first["id"], generation=first["generation"])
    _, second, _ = _enqueue(client)
    busy = client.get(f"/api/readiness/runs/{second['id']}").json()
    assert busy["reason"] == "root_group_busy" and busy["claimable"] is False

    services.jobs.finish(first["id"], "readiness-worker", status="completed", outcome={"answer": "已完成"})
    chat, third, _ = _enqueue(client)
    assert services.jobs.claim("readiness-worker", run_id=third["id"], generation=third["generation"])
    services.jobs.finish(third["id"], "readiness-worker", status="interrupted",
                         outcome={"kind": "waiting_approval"})
    blocked = client.post(f"/api/conversations/{chat['id']}/messages", json={
        "content": "同会话后续任务", "request_key": "readiness-followup",
    })
    assert blocked.status_code == 202
    pending = client.get(f"/api/readiness/runs/{blocked.json()['id']}").json()
    assert pending["reason"] == "conversation_pending" and pending["claimable"] is False
    assert pending["href"].startswith("/chat/")

    assert client.get("/api/readiness/runs/missing-run").status_code == 404


def test_errors_expose_stable_code_request_id_and_action(client):
    """错误分类只增加字段：原文案、状态码和是否可重试都不改变。"""
    missing = client.get("/api/runs/does-not-exist")
    assert missing.status_code == 404
    body = missing.json()
    assert body["code"] == "not_found" and body["action"]
    assert body["request_id"]
    assert missing.headers["X-Request-Id"] == body["request_id"]

    _, run, _ = _enqueue(client)
    conflict = client.post(f"/api/activity/runs/{run['id']}/read", json={"version": 999})
    assert conflict.status_code == 409
    body = conflict.json()
    assert body["code"] == "conflict" and body["action"]
    assert conflict.headers["X-Request-Id"] == body["request_id"]


def test_blocking_reason_reports_process_state_for_unfinished_run(client, services):
    """已中断/待回答的任务不冒充"排队"，而是给出原处理状态。"""
    _, run, _ = _enqueue(client)
    assert services.jobs.claim("readiness-worker", run_id=run["id"], generation=run["generation"])
    services.jobs.finish(run["id"], "readiness-worker", status="interrupted",
                         outcome={"kind": "execution_unknown"})
    explained = client.get(f"/api/readiness/runs/{run['id']}").json()
    assert explained["reason"] == "interrupted" and explained["claimable"] is False
    assert explained["action"]
