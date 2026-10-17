"""真实文件/图、用户API与独立进程崩溃；验证不会从未知结果自动重写。"""

import json
import subprocess
import sys

import pytest
from fixtures.reliability_provider import FileProvider
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from test_api_worker import client as _client
from test_api_worker import enqueue
from test_api_worker import services as _services
from test_workspace_runtime import enable

from knowledgeops_agent.app.runs.worker import execute_run
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.persistence.models import FileOperation

client = _client
services = _services


def prepare(client, services, tmp_path):
    services.settings.workspace_root = tmp_path / "workspace"
    path = services.settings.workspace_root / "work/review.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("old")
    enable(services.sessions)
    _, run, _ = enqueue(client)
    return run, path


def uncertain(client, services, tmp_path, monkeypatch):
    run, path = prepare(client, services, tmp_path)
    services.gateway.client, services.gateway.enabled = FileProvider(), True
    saver = InMemorySaver()
    original = FileStore.replace

    def fail(store, key, data):
        if key == "work/review.txt":
            raise ConnectionError("lost before file confirmation")
        return original(store, key, data)

    with monkeypatch.context() as patch:
        patch.setattr(FileStore, "replace", fail)
        first = execute_run(run["id"], 1, services=services, checkpointer=saver, worker_id="file-review")
    assert first["status"] == "failed"
    resumed = services.jobs.resume(run["id"])
    second = execute_run(run["id"], resumed["generation"], services=services, checkpointer=saver, worker_id="file-review")
    assert second["outcome"]["kind"] == "execution_unknown"
    receipt = client.get(f"/api/runs/{run['id']}/approvals").json()["files"][0]
    return run, path, receipt, saver


@pytest.mark.parametrize("decision", ["skip", "verify"])
def test_file_decision_uses_real_hash_and_preserves_pause(client, services, tmp_path, monkeypatch, decision):
    run, path, receipt, saver = uncertain(client, services, tmp_path, monkeypatch)
    url = f"/api/runs/{run['id']}/files/{receipt['id']}/resolve"
    body = {"expected_revision": receipt["revision"], "decision": decision}
    assert client.post(f"/api/runs/{run['id']}/resume", json={"budgets": {}}).status_code == 409
    assert client.post(url, json={**body, "expected_revision": 999}).status_code == 409
    if decision == "verify":
        assert client.post(url, json=body).status_code == 409
        assert path.read_text() == "old"
        path.write_text("new")  # 外部真人核对/修订，verify本身不做写入。
    else:
        path.write_text("human version")
    timestamp = path.stat().st_mtime_ns
    client.post(f"/api/runs/{run['id']}/pause")
    assert client.post(url, json=body).status_code == 200
    assert client.post(url, json=body).status_code == 409
    assert services.jobs.get_run(run["id"])["status"] == "paused"
    resumed = client.post(f"/api/runs/{run['id']}/resume", json={"budgets": {}}).json()
    done = execute_run(run["id"], resumed["generation"], services=services, checkpointer=saver, worker_id="file-review")
    assert done["status"] == "completed"
    assert path.stat().st_mtime_ns == timestamp
    assert path.read_text() == ("new" if decision == "verify" else "human version")
    with services.sessions() as session:
        row = session.scalar(select(FileOperation))
        assert row.status == ("succeeded" if decision == "verify" else "skipped")
        assert row.result["user_decision"]["actor"] == "user"
        if decision == "skip":
            assert row.result["message"]["status"] == "error"


def test_file_review_scope_cancel_and_changed_path(client, services, tmp_path, monkeypatch):
    run, path, receipt, _ = uncertain(client, services, tmp_path, monkeypatch)
    _, other, _ = enqueue(client)
    body = {"expected_revision": receipt["revision"], "decision": "skip"}
    assert client.post(f"/api/runs/{other['id']}/files/{receipt['id']}/resolve", json=body).status_code == 404
    client.post(f"/api/runs/{run['id']}/cancel")
    url = f"/api/runs/{run['id']}/files/{receipt['id']}/resolve"
    assert client.post(url, json=body).status_code == 403
    reopened = client.post(f"/api/runs/{run['id']}/resume", json={"budgets": {}}).json()
    assert reopened["status"] == "interrupted" and reopened["outcome"]["kind"] == "execution_unknown"
    services.settings.workspace_root = tmp_path / "other-workspace"
    inspected = client.get(f"/api/runs/{run['id']}/approvals").json()["files"][0]
    assert inspected["inspection_error"] and not inspected["matches_expected"]
    assert client.post(url, json={**body, "decision": "verify"}).status_code == 409
    assert client.post(url, json=body).status_code == 200
    assert path.read_text() == "old"


@pytest.mark.parametrize("stage", ["file-after", "file-before"])
def test_real_process_loss_recovers_or_requires_human_without_rewrite(client, services, tmp_path, stage):
    run, path = prepare(client, services, tmp_path)
    with services.engine.connect() as connection:
        schema = connection.exec_driver_sql("SELECT current_schema()").scalar_one()

    def launch(generation, mode):
        return subprocess.run([
            sys.executable, "tests/reliability_process.py", schema, services.gateway.ledger.scope,
            str(tmp_path), run["id"], str(generation), mode,
        ], capture_output=True, text=True, timeout=150)

    first = launch(1, stage)
    assert first.returncode == 73, first.stderr[-3000:]
    services.jobs.recover_on_startup()
    stopped = services.jobs.get_run(run["id"])
    assert stopped["status"] == "interrupted"  # 重启不自动运行
    if stage == "file-before":
        path.write_text("human version")
    timestamp = path.stat().st_mtime_ns
    resumed = services.jobs.resume(run["id"])
    second = launch(resumed["generation"], "file-resume")
    assert second.returncode == 0, second.stderr[-3000:]
    outcome = json.loads(second.stdout)
    if stage == "file-before":
        assert outcome["run"]["outcome"]["kind"] == "execution_unknown"
        assert outcome["model_requests"] == 0
        receipt = client.get(f"/api/runs/{run['id']}/approvals").json()["files"][0]
        assert receipt["status"] == "conflict"
        reply = client.post(f"/api/runs/{run['id']}/files/{receipt['id']}/resolve", json={
            "expected_revision": receipt["revision"], "decision": "skip",
        })
        assert reply.status_code == 200
        resumed = services.jobs.get_run(run["id"])
        third = launch(resumed["generation"], "file-resume")
        assert third.returncode == 0, third.stderr[-3000:]
        assert json.loads(third.stdout)["run"]["status"] == "completed"
        assert not (tmp_path / "file-writes.log").exists()
    else:
        assert outcome["run"]["status"] == "completed"
        assert outcome["model_requests"] == 1  # 只生成回执后的最终回答
        assert (tmp_path / "file-writes.log").read_text().splitlines() == ["write"]
    assert path.stat().st_mtime_ns == timestamp
