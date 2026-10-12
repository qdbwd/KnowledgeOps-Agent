"""真实 python -m 启动方式的回归，不让依赖注入单测掩盖模块重复加载。"""

import os
import socket
import subprocess
import sys
import time
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import text

from knowledgeops_agent.jobs import RunService
from knowledgeops_agent.task_queue import TASK_NAME, create_queue


def test_worker_entrypoint_uses_configured_log_level(tmp_path, monkeypatch):
    """在入口接缝替换服务与消费者，确认文件配置传到 Celery，不连接真实队列。"""
    from knowledgeops_agent import worker
    from knowledgeops_agent.config import Settings

    path = tmp_path / ".env"
    path.write_text("APP_LOG_LEVEL=error\nMODEL_CALLS_ENABLED=false\n", encoding="utf-8")
    monkeypatch.delenv("APP_LOG_LEVEL", raising=False)
    settings = Settings(_env_file=path, model_calls_enabled=False)
    calls = []
    services = SimpleNamespace(
        settings=settings,
        engine=object(),
        jobs=SimpleNamespace(recover_on_startup=lambda: 0, live_check=None),
        close=lambda: None,
    )
    monkeypatch.setattr(worker, "_services", None)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "create_services", lambda _: services)
    monkeypatch.setattr(
        worker, "worker_lease", lambda _: nullcontext(SimpleNamespace(check=lambda: None))
    )
    monkeypatch.setattr(
        worker, "create_queue", lambda *args, **kwargs: SimpleNamespace(worker_main=calls.append)
    )
    worker.main()
    assert len(calls) == 1
    assert "--loglevel=ERROR" in calls[0]


def test_module_entrypoint_executes_a_real_broker_delivery(business_sessions, tmp_path):
    engine = business_sessions.kw["bind"]
    with engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    assert engine.url.database == "knowledgeops_test" and schema.startswith("test_")
    url = engine.url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(
        hide_password=False
    )
    environment = {
        **os.environ,
        "DATABASE_URL": url,
        "ACCOUNTING_DATABASE_URL": url,
        "MODEL_CALLS_ENABLED": "false",
        "SOURCE_STORAGE_ROOT": str(tmp_path / "sources"),
        "CELERY_BROKER_URL": os.environ["TEST_REDIS_URL"],
    }
    queue = create_queue(environment["CELERY_BROKER_URL"])
    jobs = RunService(business_sessions)
    with (tmp_path / "worker.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "knowledgeops_agent.worker"],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            # Windows绑定挂载的项目.venv冷导入已实测约47秒；等待真实就绪信号，
            # 不把固定睡眠或进程存在当作消费者可用。
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                assert process.poll() is None, "worker exited before becoming ready"
                if queue.control.ping(destination=[f"celery@{socket.gethostname()}"], timeout=0.5):
                    break
            else:
                raise AssertionError("worker did not become ready")
            conversation = jobs.create_conversation()
            run = jobs.enqueue(
                conversation["id"], "入口回归，不允许模型调用", request_key=str(uuid4())
            )
            queue.send_task(TASK_NAME, args=[run["id"], run["generation"]])
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                state = jobs.get_run(run["id"])
                if state["status"] == "failed":
                    break
                time.sleep(0.2)
            assert state["status"] == "failed", (
                "registered task did not reach the initialized worker services"
            )
            assert "disabled" in state["stop_reason"]
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
