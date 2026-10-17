"""worker 本地执行管理，接到 Deep Agents 的 SandboxBackendProtocol。

进程组与有界管道参考 DeerFlow f71999d9 的 LocalSandbox（见 vendor/deerflow/
README-sandbox.md）。这里适配项目的同步取消/租约/预算检查；Linux selector
负责排空管道，不为每次工具调用遗留后台读取线程。本地模式不是 OS 安全隔离。
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from deepagents.backends.protocol import ExecuteResponse

from knowledgeops_agent.harness.sandbox.output import active_output
from knowledgeops_agent.vendor.deerflow.bounded_capture import BoundedPipeCapture

CAPTURE_LIMIT_BYTES = 64000
GUARD_INTERVAL_SECONDS = 1.0


def _stop_group(process: subprocess.Popen) -> bool:
    """收回本次命令的普通子进程；命令主进程已退出时仍检查原进程组。

    start_new_session=True 保证组 ID 就是 pid，不再临时查询已退出的组长。
    SIGKILL 与上游超时清理一致，避免忽略 TERM 的脚本继续运行。主动 setsid 的
    进程不属于此组；要防范不可信代码逃逸，应使用独立 OS 沙盒，不能靠本函数。
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False


def execute_local_command(
    command: str,
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
    guard: Callable[[], object],
) -> ExecuteResponse:
    """运行一次有限时长的命令，返回 SDK 响应或传播停止运行的控制异常。

    输入由 WorkspaceBackend 校验。guard 在启动前、运行中约每秒及结束后执行；
    停止/失去租约时先回收进程，再让 Runtime/worker 保存正确的任务终态。
    命令退出码、超时和输出截断会交给模型；用户取消不能包装成可重试的普通错误。
    文件写入保留，进程和 shell 变量不跨调用保留，不支持常驻后台服务。
    """
    guard()
    archive = active_output.get()
    capture = archive if archive is not None else BoundedPipeCapture(CAPTURE_LIMIT_BYTES)
    # 单管道合并 stdout/stderr，避免分别等待造成管道互相阻塞；不继承 stdin。
    try:
        process = subprocess.Popen(
            ["/bin/bash", "--noprofile", "--norc", "-c", command],
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            start_new_session=True,
        )
    except OSError as exc:
        if archive is not None:
            archive.finish(exit_code=1, stop_reason="start_failed")
        return ExecuteResponse(output=f"Cannot start local command: {exc}", exit_code=1)

    assert process.stdout is not None
    timed_out = False
    background_stopped = False
    deadline = time.monotonic() + timeout
    next_guard = time.monotonic() + GUARD_INTERVAL_SECONDS
    stop_reason = None
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while process.poll() is None:
                now = time.monotonic()
                if now >= deadline:
                    timed_out = True
                    break
                if now >= next_guard:
                    guard()
                    next_guard = time.monotonic() + GUARD_INTERVAL_SECONDS
                # 每次只读一个有界块，使持续大量输出也无法饿死取消和时限检查。
                for key, _ in selector.select(timeout=min(0.1, max(0.0, deadline - now))):
                    chunk = os.read(key.fd, 65536)
                    if chunk:
                        capture.append(chunk)
                    else:
                        selector.unregister(key.fileobj)
    except BaseException as exc:
        stop_reason = type(exc).__name__
        raise
    finally:
        try:
            # 普通后台子进程也属于工具生命周期，不能在回执/知识同步之后继续写文件。
            stopped = _stop_group(process)
            background_stopped = stopped and not timed_out and process.poll() is not None
            process.wait(timeout=5)
        finally:
            try:
                # 已结束进程可能还有管道尾部输出。限制排空时间，防止脱组写入者挂住工具。
                drain_deadline = time.monotonic() + 0.2
                while time.monotonic() < drain_deadline:
                    try:
                        chunk = os.read(process.stdout.fileno(), 65536)
                    except BlockingIOError:
                        break
                    if not chunk:
                        break
                    capture.append(chunk)
            finally:
                process.stdout.close()
                if archive is not None:
                    archive.finish(exit_code=124 if timed_out else process.returncode,
                                   stop_reason="timeout" if timed_out else stop_reason,
                                   background_stopped=background_stopped)

    guard()
    output = capture.read()
    if timed_out:
        output += (
            f"\n[Command timed out after {timeout}s; process group terminated. Exit code: 124]"
        )
    elif background_stopped:
        output += "\n[Background processes stopped at tool completion; run finite jobs in the foreground.]"
    return ExecuteResponse(
        output=output or "<no output>",
        exit_code=124 if timed_out else process.returncode,
        truncated=capture.truncated,
    )
