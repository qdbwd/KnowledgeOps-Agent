"""Celery/Redis 与事务外盒的真实边界，不使用 eager 模式冒充消息投递。"""

import importlib
import os
from threading import Event
from uuid import uuid4

import pytest
from celery.contrib.testing.worker import start_worker
from sqlalchemy import select, text

from knowledgeops_agent.jobs import RunService


def test_explicit_broker_isolates_connections_from_parent_environment(monkeypatch):
    """测试实例的 broker 参数应生效，不能意外连接开发进程的 DB 0。"""
    queue_module = importlib.import_module("knowledgeops_agent.task_queue")
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://redis:6379/0")
    app = queue_module.create_queue("redis://redis:6379/15")
    try:
        assert app.connection_for_read().as_uri().endswith("/15")
        assert app.connection_for_write().as_uri().endswith("/15")
    finally:
        app.close()


def make_run(sessions):
    service = RunService(sessions)
    conversation = service.create_conversation("队列验证")
    return service, service.enqueue(conversation["id"], "测试任务", request_key=str(uuid4()))


def test_outbox_survives_publish_failure_without_losing_queued_run(business_sessions):
    queue_module = importlib.import_module("knowledgeops_agent.task_queue")
    db = importlib.import_module("knowledgeops_agent.db")
    service, run = make_run(business_sessions)

    class FailingBroker:
        def send_task(self, *_args, **_kwargs):
            raise ConnectionError("broker unavailable")

    publisher = queue_module.OutboxPublisher(business_sessions, FailingBroker())
    assert publisher.publish_due() == 0
    assert service.get_run(run["id"])["status"] == "queued"
    with business_sessions() as session:
        outbox = session.scalar(select(db.RunDispatch).where(db.RunDispatch.run_id == run["id"]))
        assert outbox.attempts == 1 and outbox.published_at is None
        assert outbox.last_error == "ConnectionError"


def test_redis_delivery_and_duplicate_do_not_execute_twice(business_sessions):
    queue_module = importlib.import_module("knowledgeops_agent.task_queue")
    service, run = make_run(business_sessions)
    queue_name = "test-" + uuid4().hex
    executed = []
    deliveries = []
    complete = Event()
    seen_duplicate = Event()

    def handler(run_id, generation):
        deliveries.append((run_id, generation))
        if len(deliveries) == 2:
            seen_duplicate.set()
        claim = service.claim("integration-worker", run_id=run_id, generation=generation)
        if claim:
            executed.append(run_id)
            service.finish(run_id, "integration-worker", outcome={"kind": "test"})
            complete.set()

    app = queue_module.create_queue(
        os.environ["TEST_REDIS_URL"], handler=handler, queue_name=queue_name
    )
    assert app.conf.task_always_eager is False
    with start_worker(
        app, pool="solo", concurrency=1, perform_ping_check=False, shutdown_timeout=10
    ):
        publisher = queue_module.OutboxPublisher(business_sessions, app)
        assert publisher.publish_due() == 1
        assert complete.wait(8), "real Redis message was not consumed"
        # 重复消息仍经真实 broker；队列标记用第二个事件确认已消费它。
        app.send_task(queue_module.TASK_NAME, args=[run["id"], run["generation"]])
        assert seen_duplicate.wait(8)
    app.close()
    assert len(deliveries) == 2
    assert executed == [run["id"]]


def test_old_delivery_cannot_restart_interrupted_or_new_generation(business_sessions):
    service, run = make_run(business_sessions)
    generation = run["generation"]
    service.recover_on_startup()
    assert service.claim("worker", run_id=run["id"], generation=generation) is None
    resumed = service.resume(run["id"])
    assert resumed["generation"] == generation + 1
    assert service.claim("worker", run_id=run["id"], generation=generation) is None
    assert (
        service.claim("worker", run_id=run["id"], generation=resumed["generation"])["id"]
        == run["id"]
    )


def test_worker_lease_rejects_two_active_worker_processes(business_sessions):
    queue_module = importlib.import_module("knowledgeops_agent.task_queue")
    engine = business_sessions.kw["bind"]
    with queue_module.worker_lease(engine):
        with pytest.raises(RuntimeError, match="worker"):
            with queue_module.worker_lease(engine):
                pytest.fail("second worker unexpectedly acquired the lease")


def test_lost_lease_does_not_become_valid_after_connection_reconnect(business_sessions):
    queue_module = importlib.import_module("knowledgeops_agent.task_queue")
    engine = business_sessions.kw["bind"]
    with queue_module.worker_lease(engine) as lease:
        lease.check()
        lease.connection.invalidate()
        lease.connection.rollback()
        with pytest.raises(RuntimeError, match="lease"):
            lease.check()
        # 失效被锁存；即使数据库又能连接，旧消费者也不能重新取得工作权限。
        with pytest.raises(RuntimeError, match="lease"):
            lease.check()


def test_lease_check_detects_unlocked_same_database_connection(business_sessions):
    queue_module = importlib.import_module("knowledgeops_agent.task_queue")
    with queue_module.worker_lease(business_sessions.kw["bind"]) as lease:
        lease.connection.execute(text("SELECT pg_advisory_unlock_all()"))
        with pytest.raises(RuntimeError, match="lease"):
            lease.check()
