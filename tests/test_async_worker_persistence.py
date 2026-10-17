"""独立进程 + 真实 Celery/Redis/PG；供应商用屏障，任何收费客户端均不参与。"""

import os
import subprocess
import sys
import time
from uuid import uuid4

from sqlalchemy import text
from test_api_worker import services as _services
from test_async_run_graph import configure

from knowledgeops_agent.app.runs.children import ChildRuns
from knowledgeops_agent.app.runs.queue import OutboxPublisher, create_queue

services = _services


def test_real_three_slot_worker_and_new_process_resume_keep_identity_and_budget(services, tmp_path):
    with services.engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    assert schema.startswith("test_") and services.engine.url.database == "knowledgeops_test"
    queue = create_queue(os.environ["TEST_REDIS_URL"], queue_name="async-test-" + uuid4().hex)
    publisher = OutboxPublisher(services.sessions, queue)
    ready = tmp_path / "ready"
    log = (tmp_path / "async-worker.log").open("w")
    process = None

    def launch():
        ready.unlink(missing_ok=True)
        child = subprocess.Popen([sys.executable, "tests/async_worker_process.py", schema,
                                  services.gateway.ledger.scope, str(tmp_path), queue.conf.task_default_queue],
                                 stdout=log, stderr=subprocess.STDOUT,
                                 env={**os.environ, "MODEL_CALLS_ENABLED": "false"})
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            assert child.poll() is None, "offline worker exited before ready"
            if ready.exists():
                return child
            time.sleep(.1)
        child.terminate()
        child.wait(timeout=15)
        raise AssertionError("offline worker did not become ready")

    def wait_status(identifier, expected, *, publish):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            assert process.poll() is None
            if publish:
                publisher.publish_due()
            run = services.jobs.get_run(identifier)
            if run["status"] == expected:
                return run
            assert run["status"] not in {"failed", "budget_exhausted"}, run
            time.sleep(.05)
        raise AssertionError(f"task did not become {expected}: {run}")

    try:
        process = launch()
        first = configure(services)
        completed = wait_status(first["id"], "completed", publish=True)
        children = ChildRuns(services.jobs).list(first["id"])
        overlap = [e for e in services.jobs.events(first["id"]) if e["kind"] == "offline_overlap"]
        assert overlap and set(overlap[0]["data"]["arrived"]) == {first["id"], *(c["task_id"] for c in children)}
        assert len(children) == 2 and all(c["status"] == "completed" for c in children)
        assert completed["generation"] >= 2

        conversation = services.jobs.create_conversation("跨进程接续")
        second = services.jobs.enqueue(conversation["id"], "重启后整合两份独立报告", request_key="restart")
        publisher.publish_due()  # 只投递主运行，子 dispatch 留待重启后发出。
        waiting = wait_status(second["id"], "waiting_children", publish=False)
        original_children = ChildRuns(services.jobs).list(second["id"])
        assert len(original_children) == 2 and all(c["status"] == "queued" for c in original_children)
        used_before = services.gateway.ledger.summary(second["id"])["root_used"]
        process.terminate()
        process.wait(timeout=20)
        process = launch()
        stopped = services.jobs.get_run(second["id"])
        assert stopped["status"] == "interrupted"
        assert all(c["status"] == "interrupted" for c in ChildRuns(services.jobs).list(second["id"]))
        assert publisher.publish_due() == 0
        assert services.gateway.ledger.summary(second["id"])["root_used"] == used_before
        resumed = services.jobs.resume(second["id"])
        assert resumed["active_ms"] >= waiting["active_ms"]
        final = wait_status(second["id"], "completed", publish=True)
        assert final["id"] == second["id"] and final["budgets"] == second["budgets"]
        assert {c["task_id"] for c in ChildRuns(services.jobs).list(second["id"])} == {c["task_id"] for c in original_children}
        assert services.gateway.ledger.summary(second["id"])["root_used"] == used_before + 3  # 两子响应及主整合，无首轮重放。
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        queue.close()
        log.close()
