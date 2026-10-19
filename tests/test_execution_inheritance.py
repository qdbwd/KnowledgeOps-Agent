"""会话默认、自动推进、子运行与异常重启使用同一持久配置合同。"""

from uuid import uuid4

from sqlalchemy import select
from test_api_worker import client as _client
from test_api_worker import services as _services
from test_reasoning_runtime import offline_reasoning

from knowledgeops_agent.learning.tasks import LearningTaskService
from knowledgeops_agent.persistence.models import CommandExecution, LearningAdvance

client = _client
services = _services


def test_conversation_defaults_freeze_for_human_child_and_automatic_roots(services, client):
    services.reasoning = offline_reasoning()
    conv = client.post("/api/conversations", json={}).json()["id"]
    url = f"/api/conversations/{conv}/runtime-settings"
    assert (
        client.patch(
            url, json={"expected_revision": 0, "permission_mode": "auto_review", "reasoning_effort": "max"}
        ).status_code
        == 200
    )
    body = {"content": "整理事务", "request_key": str(uuid4())}
    first = client.post(f"/api/conversations/{conv}/messages", json=body).json()
    assert first["input"]["reasoning"]["effective"] == "max"
    assert first["input"]["runtime_settings"]["permission_mode"] == "auto_review"
    services.jobs.claim("worker", run_id=first["id"], generation=1)
    child = services.jobs.start_child(first["id"], "读取资料", operation_key="child")
    assert child["input"]["reasoning"] == first["input"]["reasoning"]
    assert (
        client.patch(
            url, json={"expected_revision": 1, "reasoning_effort": "low", "permission_mode": "ask"}
        ).status_code
        == 200
    )
    assert services.jobs.get_run(first["id"])["input"]["reasoning"]["effective"] == "max"
    # 原请求重试不受更新默认值影响，仍返回最初保存的运行。
    assert client.post(f"/api/conversations/{conv}/messages", json=body).json()["id"] == first["id"]
    assert services.jobs.claim("child-worker", run_id=child["id"], generation=child["generation"])
    services.jobs.finish(child["id"], "child-worker")
    services.jobs.finish(first["id"], "worker")
    tasks = LearningTaskService(services.sessions)
    task = tasks.create(
        conversation_id=conv,
        request_key="learning",
        title="事务",
        goal="理解事务",
        mode="delegated",
        units=[{"key": "one", "title": "事务", "goal": "原子性"}],
    )
    tasks.confirm(task["id"], expected_revision=1, auto_publish=True)
    automatic = next(r for r in services.jobs.list_runs() if r["origin"] == "automatic")
    assert automatic["input"]["reasoning"]["effective"] == "low"
    assert automatic["input"]["runtime_settings"]["permission_mode"] == "ask"
    services.jobs.claim("worker", run_id=automatic["id"], generation=1)
    services.jobs.finish(
        automatic["id"], "worker", status="interrupted", outcome={"kind": "waiting_approval"}
    )
    from knowledgeops_agent.learning.coordinator import advance

    with services.sessions.begin() as session:
        assert advance(session, conv) is None
        assert len(list(session.scalars(select(LearningAdvance)))) == 1


def test_worker_restart_preserves_unknown_commands_without_dispatch(services):
    conv = services.jobs.create_conversation()
    run = services.jobs.enqueue(conv["id"], "运行命令", request_key="restart")
    services.jobs.claim("worker", run_id=run["id"], generation=1)
    with services.sessions.begin() as session:
        session.add(
            CommandExecution(
                root_id=run["id"],
                run_id=run["id"],
                tool_call_id="unknown",
                fingerprint="a" * 64,
                request={
                    "name": "execute",
                    "args": {"command": "touch done"},
                    "workspace": "/work",
                    "cwd": "/work",
                },
                authority_epoch=0,
                status="running",
                attempt=1,
                revision=1,
            )
        )
    services.jobs.recover_on_startup()
    row = services.jobs.get_run(run["id"])
    assert row["status"] == "interrupted" and row["outcome"]["kind"] == "execution_unknown"
    with services.sessions() as session:
        receipt = session.scalar(
            select(CommandExecution).where(CommandExecution.root_id == run["id"])
        )
        assert receipt.status == "unknown" and receipt.revision == 2
    assert services.jobs.claim("new-worker", run_id=run["id"], generation=1) is None
