"""HTTP和事务控制共同验证引导，不能由前端自行更改任务运行状态。"""

import pytest
from test_api_worker import client as _client
from test_api_worker import services as _services
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

client = _client
services = _services
runtime_services = _runtime_services


def test_http_guidance_targets_exact_running_generation(client, services):
    jobs = services.jobs
    cid = jobs.create_conversation()["id"]
    active = jobs.enqueue(cid, "开始研究", request_key="active")
    jobs.claim("w", run_id=active["id"], generation=1)
    queued = jobs.enqueue(cid, "等等", request_key="queued")
    url = f"/api/runs/{queued['id']}/guide"
    assert (
        client.post(url, json={"target_root_id": active["id"], "target_generation": 2}).status_code
        == 409
    )
    assert jobs.get_run(queued["id"])["status"] == "queued"
    response = client.post(url, json={"target_root_id": active["id"], "target_generation": 1})
    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    listed = client.get(f"/api/runs/{active['id']}/guidance").json()
    assert listed == [response.json()]
    assert client.get(f"/api/runs/{active['id']}/records").json()["guidance"] == listed


def test_old_checkpoint_tool_decision_is_rejected_after_guidance_applied(runtime_services):
    from knowledgeops_agent.guidance import apply_decision
    from knowledgeops_agent.jobs import SteeringPending

    _, context, run = setup_run(runtime_services)
    queued = context.jobs.enqueue(
        run["conversation_id"], "改变任务", request_key="stale-checkpoint"
    )
    delivery = context.jobs.guide(queued["id"], target_id=run["id"], target_generation=1)
    apply_decision(context, delivery, {"mode": "preview", "published_ids": []})
    with context.jobs.sessions.begin() as session:
        with pytest.raises(SteeringPending):
            context.jobs.publication_guard(session, run["id"], "worker-test", authority_epoch=0)
        context.jobs.publication_guard(session, run["id"], "worker-test", authority_epoch=1)
