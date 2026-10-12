"""在开发容器内启动 API、Celery worker 和 Vite，退出时回收三个进程组。

从 /workspace 执行 ``python scripts/dev.py``。Docker/数据库的生命周期仍由
Compose 管理；本入口只启动当前容器中的应用进程，不安装依赖、不调用 Docker。
API/Vite 自带热更新；worker 保持显式重启，避免保存代码时中断长任务。
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]


def build_environment(env_file: Path, inherited: Mapping[str, str]) -> dict[str, str]:
    """每次启动重新读取文件，返回子进程环境，不污染启动器自身的环境。

    开发 Compose 不注入应用配置。根据当前文件生成基础服务连接及固定内部路径。
    显式进程覆盖仍高于文件，例如 MODEL_CALLS_ENABLED=false 可保证离线验证。
    删除文件中的可选字段后，下次启动也不会从上一次启动器中复活旧值。
    """
    from dotenv import dotenv_values

    if not env_file.is_file():
        raise FileNotFoundError("项目根 .env 不存在，请先根据 .env.example 完成配置。")
    environment = {
        key: value for key, value in dotenv_values(env_file, encoding="utf-8").items()
        if value is not None
    }
    environment.update(inherited)
    # Compose 拓扑仍使用 postgres/redis 固定服务名；每次从当前密码生成连接，
    # 显式 DSN 覆盖可用于调试/隔离测试。密码作为 URL 组件编码，不能直接拼接。
    password = environment.get("POSTGRES_PASSWORD", "")
    databases = {
        "DATABASE_URL": "knowledgeops", "ACCOUNTING_DATABASE_URL": "knowledgeops_accounting",
        "TEST_DATABASE_URL": "knowledgeops_test",
    }
    for variable, database in databases.items():
        if variable not in environment:
            if not password:
                raise ValueError("POSTGRES_PASSWORD 未配置，无法生成开发数据库连接。")
            environment[variable] = (
                f"postgresql+psycopg://knowledgeops:{quote(password, safe='')}@postgres:5432/{database}"
            )
    environment.setdefault("CELERY_BROKER_URL", "redis://redis:6379/0")
    environment.setdefault("TEST_REDIS_URL", "redis://redis:6379/15")
    # 与 Compose 工作区挂载及备份合同一致；不把用户填错的内部目录变成另一个工作区。
    for variable, default in {
        "WORKSPACE_ROOT": "/data/agent-workspace",
        "SOURCE_STORAGE_ROOT": "/data/agent-workspace/uploads/originals",
    }.items():
        environment[variable] = inherited.get(variable, default)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["WATCHFILES_FORCE_POLLING"] = environment.get("DEV_WATCH_POLLING", "true")
    environment["API_PROXY_TARGET"] = "http://127.0.0.1:8000"
    return environment


@dataclass(frozen=True)
class Service:
    """一个本地进程的名称、参数和工作目录；不是 Compose 服务声明。"""

    name: str
    command: list[str]
    directory: Path


def stop_processes(processes: list[subprocess.Popen], timeout: float) -> None:
    """先允许进程善后，再回收超时进程及 reload/npm 创建的后代。"""
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    for process in processes:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    # 即使父进程先退出，也不能留下 npm/Vite 或 Uvicorn reload 的子进程。
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_services(
    services: list[Service], environment: dict[str, str], *, shutdown_timeout: float = 20
) -> int:
    """前台监督应用；中断、启动失败或任一进程退出都统一收尾，不自动重试任务。"""
    stopping = Event()
    exit_code = 0
    processes: list[subprocess.Popen] = []

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal exit_code
        exit_code = 128 + signum
        stopping.set()

    original_handlers = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        for service in services:
            if stopping.is_set():
                break
            print(f"[dev] 启动 {service.name}：{' '.join(service.command)}", flush=True)
            processes.append(subprocess.Popen(
                service.command, cwd=service.directory, env=environment, start_new_session=True
            ))
        print("[dev] 启动中，请等待 API/worker 就绪日志。Ctrl+C 停止全部应用进程。", flush=True)
        while not stopping.wait(0.2):
            for service, process in zip(services, processes, strict=False):
                result = process.poll()
                if result is not None:
                    print(
                        f"[dev] {service.name} 已退出（状态 {result}），停止其余进程。",
                        file=sys.stderr, flush=True,
                    )
                    return result if result > 0 else 1
        return exit_code
    finally:
        print("[dev] 正在停止应用进程；数据库和开发容器继续保留。", flush=True)
        try:
            stop_processes(processes, shutdown_timeout)
        finally:
            for signum, handler in original_handlers.items():
                signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    """校验开发环境后使用项目 Linux 虚拟环境启动；不改变模型开关。"""
    if os.name != "posix" or os.environ.get("KNOWLEDGEOPS_DEV_CONTAINER") != "1":
        print("请进入本项目开发容器，在 /workspace 运行 python scripts/dev.py。", file=sys.stderr)
        return 1
    if os.environ.get("KNOWLEDGEOPS_DEV_CONFIG_SOURCE") != "dotenv-v2":
        print("请先按新版 compose.dev.yaml 重新创建一次 dev，移除旧应用环境快照。",
              file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", nargs=argparse.REMAINDER,
                        help="加载同一开发配置后执行单条调试/测试命令，不启动三个应用进程")
    arguments = parser.parse_args(argv)
    if arguments.run == []:
        parser.error("--run 后需要命令，例如 --run python -m pytest tests/test_config.py")
    try:
        environment = build_environment(ROOT / ".env", os.environ)
    except (OSError, ValueError) as error:
        print(f"[dev] 配置读取失败：{error}", file=sys.stderr)
        return 1
    if arguments.run is not None:
        # 替换为单条前台命令，保留当前目录、信号及退出码；不持有应用启动锁。
        try:
            os.execvpe(arguments.run[0], arguments.run, environment)
        except OSError as error:
            print(f"[dev] 命令启动失败：{error}", file=sys.stderr)
            return 1

    import fcntl

    python = ROOT / ".venv/bin/python"
    npm = shutil.which("npm")
    if not python.is_file() or not npm or not (ROOT / "frontend/node_modules/vite").is_dir():
        print("开发依赖尚未准备好，请检查 init 容器的 bootstrap 日志。", file=sys.stderr)
        return 1
    # 锁只在当前开发容器有效；跨容器重复消费者仍由 worker 的 PG 租约拒绝。
    with Path("/tmp/knowledgeops-dev.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("当前开发容器已有启动脚本运行，请先在原终端 Ctrl+C。", file=sys.stderr)
            return 1
        for port in (8000, 5173):
            try:
                with socket.socket() as connection:
                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    connection.bind(("0.0.0.0", port))
            except OSError:
                print(f"开发端口 {port} 已被占用，请停止原应用进程后重试。", file=sys.stderr)
                return 1

        services = [
            Service("API", [
                str(python), "-m", "uvicorn", "knowledgeops_agent.api:app",
                "--host", "0.0.0.0", "--port", "8000", "--reload",
                "--reload-dir", "backend/src", "--log-level", environment.get("APP_LOG_LEVEL", "info"),
                "--timeout-graceful-shutdown", "5",
            ], ROOT),
            Service("worker", [str(python), "-m", "knowledgeops_agent.worker"], ROOT),
            Service("Vite", [npm, "run", "dev", "--", "--port", "5173", "--strictPort"], ROOT / "frontend"),
        ]
        print(f"[dev] 配置的网页地址：http://localhost:{environment.get('KNOWLEDGEOPS_PORT', '8787')}"
              "（修改端口需重新创建 dev）", flush=True)
        try:
            return run_services(services, environment)
        except OSError as error:
            print(f"[dev] 启动失败：{error}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
