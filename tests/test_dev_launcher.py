"""用真实轻量进程验证开发入口的退出边界，不启动模型或访问业务数据。"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_each_launch_loads_current_file_without_changing_parent_environment(tmp_path):
    from scripts.dev import build_environment

    path = tmp_path / ".env"
    parent = {"PATH": "/example/bin", "DATABASE_URL": "postgresql://internal/db",
              "POSTGRES_PASSWORD": "fictional-password"}
    path.write_text("APP_LOG_LEVEL=warning\nMODEL_TIMEOUT_SECONDS=31\n"
                    "SKILLS_DIRECTORY=/example/skills\nDEV_WATCH_POLLING=false\n",
                    encoding="utf-8")
    first = build_environment(path, parent)
    path.write_text("APP_LOG_LEVEL=debug\nMODEL_TIMEOUT_SECONDS=63\n", encoding="utf-8")
    second = build_environment(path, parent)
    assert first["APP_LOG_LEVEL"] == "warning"
    assert second["APP_LOG_LEVEL"] == "debug"
    assert second["MODEL_TIMEOUT_SECONDS"] == "63"
    assert "SKILLS_DIRECTORY" not in second
    assert first["WATCHFILES_FORCE_POLLING"] == "false"
    assert second["WATCHFILES_FORCE_POLLING"] == "true"
    assert second["API_PROXY_TARGET"] == "http://127.0.0.1:8000"
    assert second["DATABASE_URL"] == parent["DATABASE_URL"]
    assert set(parent) == {"PATH", "DATABASE_URL", "POSTGRES_PASSWORD"}


def test_explicit_offline_override_and_compose_paths_survive_dotenv(tmp_path):
    from scripts.dev import build_environment

    path = tmp_path / ".env"
    path.write_text("POSTGRES_PASSWORD=fictional-password\nMODEL_CALLS_ENABLED=true\nAPP_LOG_LEVEL=info\n"
                    "WORKSPACE_ROOT=/incorrect\nKNOWLEDGEOPS_PORT=9999\n",
                    encoding="utf-8")
    result = build_environment(path, {
        "MODEL_CALLS_ENABLED": "false", "APP_LOG_LEVEL": "error",
        "WORKSPACE_ROOT": "/data/agent-workspace", "KNOWLEDGEOPS_PORT": "8787",
    })
    assert result["MODEL_CALLS_ENABLED"] == "false"
    assert result["APP_LOG_LEVEL"] == "error"
    assert result["WORKSPACE_ROOT"] == "/data/agent-workspace"
    assert result["KNOWLEDGEOPS_PORT"] == "8787"


def test_database_defaults_use_current_password_and_encode_url_components(tmp_path):
    from sqlalchemy.engine import make_url

    from scripts.dev import build_environment

    path = tmp_path / ".env"
    for password in ("fictional@password:/#one", "fictional-two"):
        path.write_text(f"POSTGRES_PASSWORD='{password}'\n", encoding="utf-8")
        environment = build_environment(path, {})
        for variable, database in (
            ("DATABASE_URL", "knowledgeops"),
            ("ACCOUNTING_DATABASE_URL", "knowledgeops_accounting"),
            ("TEST_DATABASE_URL", "knowledgeops_test"),
        ):
            url = make_url(environment[variable])
            assert url.password == password
            assert (url.host, url.port, url.database) == ("postgres", 5432, database)
        assert environment["WORKSPACE_ROOT"] == "/data/agent-workspace"
        assert environment["CELERY_BROKER_URL"] == "redis://redis:6379/0"


def test_single_command_reads_new_file_without_recreating_container(tmp_path):
    """经真实子进程及Settings验证新文件值，而不只检查字典拼装。"""
    path = tmp_path / ".env"
    program = (
        "import json; from knowledgeops_agent.core.config import Settings; "
        "s=Settings(_env_file=None); "
        "print(json.dumps([s.app_log_level,s.model_timeout_seconds,s.model_calls_enabled]))"
    )
    driver = (
        "from pathlib import Path; import scripts.dev as d; "
        f"d.ROOT=Path({str(tmp_path)!r}); "
        f"raise SystemExit(d.main(['--run',{sys.executable!r},'-c',{program!r}]))"
    )
    inherited = {"PATH": os.environ["PATH"], "KNOWLEDGEOPS_DEV_CONTAINER": "1",
                 "KNOWLEDGEOPS_DEV_CONFIG_SOURCE": "dotenv-v2", "MODEL_CALLS_ENABLED": "false"}
    for level, timeout in (("warning", 39), ("debug", 63)):
        path.write_text(f"POSTGRES_PASSWORD=fictional-password\nMODEL_CALLS_ENABLED=true\n"
                        f"APP_LOG_LEVEL={level}\nMODEL_TIMEOUT_SECONDS={timeout}\n", encoding="utf-8")
        completed = subprocess.run([sys.executable, "-c", driver], cwd=ROOT, env=inherited,
                                   capture_output=True, text=True, timeout=30, check=True)
        assert json.loads(completed.stdout) == [level, float(timeout), False]


def test_missing_dotenv_fails_before_starting_any_service(tmp_path):
    from scripts.dev import build_environment

    with pytest.raises(FileNotFoundError):
        build_environment(tmp_path / ".env", {})


def _running(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        return False


def _launch(tmp_path: Path, failure: int | None = None) -> subprocess.Popen:
    # worker 的后代故意忽略 TERM，验证父进程退出后仍能清理同一进程组。
    grandchild = (
        "import os, signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(tmp_path / 'descendant')!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    services = []
    for name in ("api", "worker", "web"):
        program = (
            "import os, subprocess, sys, time; from pathlib import Path; "
            f"Path({str(tmp_path / name)!r}).write_text(str(os.getpid())); "
        )
        if name == "worker":
            program += f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        if name == "web" and failure is not None:
            program += f"time.sleep(1); sys.exit({failure})"
        else:
            program += "time.sleep(60)"
        services.append(f"Service({name!r}, [{sys.executable!r}, '-c', {program!r}], Path({str(tmp_path)!r}))")
    driver = (
        "import os; from pathlib import Path; from scripts.dev import Service, run_services; "
        f"raise SystemExit(run_services([{','.join(services)}], dict(os.environ), shutdown_timeout=0.4))"
    )
    return subprocess.Popen([sys.executable, "-c", driver], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM])
def test_signal_stops_all_services_and_descendants(tmp_path, stop_signal):
    runner = _launch(tmp_path)
    try:
        deadline = time.monotonic() + 10
        markers = [tmp_path / name for name in ("api", "worker", "web", "descendant")]
        while not all(path.exists() and path.read_text() for path in markers):
            assert time.monotonic() < deadline
            assert runner.poll() is None
            time.sleep(0.05)
        runner.send_signal(stop_signal)
        output = runner.communicate(timeout=10)[0]
        assert runner.returncode == 128 + stop_signal, output
        assert all(not _running(int(path.read_text())) for path in markers)
    finally:
        if runner.poll() is None:
            runner.terminate()
            runner.communicate(timeout=10)


@pytest.mark.parametrize("failure", [0, 7])
def test_unexpected_exit_stops_other_services_and_reports_failure(tmp_path, failure):
    runner = _launch(tmp_path, failure)
    try:
        output = runner.communicate(timeout=10)[0]
        assert runner.returncode == (failure or 1), output
        for name in ("api", "worker", "web", "descendant"):
            assert not _running(int((tmp_path / name).read_text()))
    finally:
        if runner.poll() is None:
            runner.terminate()
            runner.communicate(timeout=10)
