"""真实 Linux 进程验证：本地执行的取消、进程清理和输出边界。

用短命令和临时目录检查执行机制；不调用收费模型，也不启动独立沙盒容器。
"""

import os
import shlex
import time
from pathlib import Path

import pytest

from knowledgeops_agent.jobs import RunStopped
from knowledgeops_agent.skill_resources import FrozenSkillBackend
from knowledgeops_agent.workspace import WorkspaceBackend


def backend(tmp_path, **kwargs):
    return WorkspaceBackend(tmp_path, skills=FrozenSkillBackend({}), **kwargs)


def assert_process_stopped(pid_file: Path):
    """孤儿进程可能短暂处于 zombie；不可再运行才是这里需要验证的条件。"""
    pid = int(pid_file.read_text())
    stat = Path(f"/proc/{pid}/stat")
    for _ in range(50):
        if not stat.exists():
            return
        try:
            state = stat.read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            # /proc条目可能在exists和read之间被内核回收，两种错误都表示已退出。
            return
        if state == "Z":
            return
        time.sleep(0.02)
    pytest.fail(f"command process {pid} is still running")


def test_cancellation_during_command_stops_its_children(tmp_path):
    def guard():
        if (tmp_path / "started").exists():
            raise RunStopped("cancelled")

    store = backend(tmp_path, guard=guard, max_timeout=8)
    start = time.monotonic()
    with pytest.raises(RunStopped, match="cancelled"):
        store.execute("sleep 30 & echo $! > child.pid; touch started; wait")
    assert time.monotonic() - start < 4
    assert_process_stopped(tmp_path / "child.pid")


def test_timeout_kills_child_that_ignores_term_and_reports_failure(tmp_path):
    store = backend(tmp_path, max_timeout=2)
    result = store.execute(
        "bash -c 'trap \"\" TERM; echo $$ > child.pid; while :; do sleep 1; done' & wait",
        timeout=1,
    )
    assert result.exit_code == 124
    assert "timed out" in result.output.lower()
    assert_process_stopped(tmp_path / "child.pid")


def test_background_children_do_not_outlive_the_tool_call(tmp_path):
    store = backend(tmp_path, max_timeout=5)
    start = time.monotonic()
    result = store.execute("sleep 30 & echo $! > child.pid; printf foreground-finished")
    assert time.monotonic() - start < 3
    assert "foreground-finished" in result.output
    assert "background" in result.output.lower()
    assert_process_stopped(tmp_path / "child.pid")


def test_large_stdout_and_stderr_are_drained_but_not_retained(tmp_path):
    store = backend(tmp_path, max_timeout=5)
    # 输出远大于管道容量，同时包括多字节文本，不能只按 Python 字符数限制。
    script = "import sys; sys.stdout.write('文' * 800000); sys.stdout.flush(); sys.stderr.write('E' * 800000)"
    result = store.execute("python -c " + shlex.quote(script))
    assert result.exit_code == 0
    assert result.truncated
    assert len(result.output.encode("utf-8")) < 66000
    assert "discarded" in result.output
    assert "read_file" in result.output


def test_eof_stderr_exit_code_and_script_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "not-for-child-process")
    store = backend(tmp_path)
    script = "import os,sys; print(repr(sys.stdin.read())); print(os.getenv('DATABASE_URL', 'unset')); print('failure', file=sys.stderr); sys.exit(7)"
    (tmp_path / "work/job.py").write_text(script)
    result = store.execute("python work/job.py")
    assert result.exit_code == 7
    assert "''" in result.output and "unset" in result.output and "failure" in result.output
    assert not result.truncated
    assert store.execute("pwd").output.strip() == str(tmp_path)
    assert (tmp_path / "work/job.py").exists()


def test_guard_failure_does_not_leak_pipe_descriptors(tmp_path):
    def guard():
        if (tmp_path / "started").exists():
            raise RunStopped("worker owner no longer matches")

    store = backend(tmp_path, guard=guard)
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(RunStopped, match="owner"):
        store.execute("touch started; sleep 30")
    assert len(os.listdir("/proc/self/fd")) <= before


def test_invalid_command_cannot_start_shell(tmp_path):
    store = backend(tmp_path)
    assert store.execute("").exit_code == 1
    assert store.execute("true", timeout=True).exit_code == 1
