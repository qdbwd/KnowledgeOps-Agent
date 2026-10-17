"""真实本地协议验证连接的状态与进程生命周期；无公共服务、无模型调用。"""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from knowledgeops_agent.harness.mcp.sessions import MCPSessionManager
from knowledgeops_agent.harness.mcp.transport import child_environment


def stateful_binding():
    return {
        "id": "stateful",
        "authorization_epoch": 1,
        "revision": 1,
        "config": {
            "transport": "stdio",
            "protocol_mode": "legacy",
            "command": sys.executable,
            "args": [str(Path("tests/fixtures/mcp_stateful.py").resolve())],
        },
    }


@pytest.fixture(params=["stdio", "http"])
def protocol_binding(request, tmp_path):
    binding = stateful_binding()
    if request.param == "stdio":
        yield binding
        return
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (tmp_path / "stateful-http.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, *binding["config"]["args"], "--http-port", str(port)],
            env=child_environment({}),
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                assert process.poll() is None
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise AssertionError("local HTTP MCP did not start")
            binding["config"] = {
                "transport": "http",
                "url": f"http://127.0.0.1:{port}/mcp",
                "protocol_mode": "legacy",
            }
            yield binding
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


async def counter(manager, scope, binding, *, timeout=None, delay=0):
    async with asyncio.timeout(timeout):
        async with manager.lease(scope, binding) as lease:
            tools = {
                item.name: item for item in await lease.adapter.list_tools(cache_mode="bypass")
            }
            response = await tools["counter"].ainvoke(
                {
                    "name": "counter",
                    "args": {"delay": delay},
                    "id": "local-test",
                    "type": "tool_call",
                }
            )
            assert response.status == "success", response.content
            return (
                lease.session_id,
                lease.reused,
                response.artifact["structured_content"],
                lease.protocol_version,
            )


def assert_process_exited(pid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError("MCP child process was not released")


def test_real_session_state_reuse_and_scope_isolation(protocol_binding):
    with MCPSessionManager() as manager:
        first = manager.run(counter, manager, "chat-a", protocol_binding)
        second = manager.run(counter, manager, "chat-a", protocol_binding)
        other = manager.run(counter, manager, "chat-b", protocol_binding)
        assert first[0] == second[0] != other[0]
        assert not first[1] and second[1]
        assert first[3] == second[3] == "2025-11-25"
        assert [first[2]["count"], second[2]["count"], other[2]["count"]] == [1, 2, 1]
        assert first[2]["session"] == second[2]["session"] != other[2]["session"]
        manager.close_scope("chat-a")
        if protocol_binding["config"]["transport"] == "stdio":
            assert_process_exited(first[2]["pid"])
        reopened = manager.run(counter, manager, "chat-a", protocol_binding)
        assert reopened[0] != first[0] and reopened[2]["count"] == 1
    if protocol_binding["config"]["transport"] == "stdio":
        assert_process_exited(other[2]["pid"])
        assert_process_exited(reopened[2]["pid"])


def test_dead_stdio_session_fails_current_call_and_only_next_explicit_call_reconnects():
    binding = stateful_binding()
    with MCPSessionManager() as manager:
        first = manager.run(counter, manager, "chat", binding)
        os.kill(first[2]["pid"], signal.SIGTERM)
        # 等服务进程确实停止；否则一个很快的第二调用可能先于 SIGTERM 被处理。
        assert_process_exited(first[2]["pid"])
        with pytest.raises(ConnectionError):
            manager.run(counter, manager, "chat", binding)
        second = manager.run(counter, manager, "chat", binding)
        assert second[0] != first[0] and second[2]["count"] == 1


def test_timeout_closes_stdio_session_instead_of_retaining_unknown_state():
    binding = stateful_binding()
    with MCPSessionManager() as manager:
        first = manager.run(counter, manager, "chat", binding)

        async def timed():
            return await counter(manager, "chat", binding, timeout=0.3, delay=10)

        with pytest.raises(TimeoutError):
            manager.run(timed)
        assert_process_exited(first[2]["pid"])
        second = manager.run(counter, manager, "chat", binding)
        assert second[0] != first[0] and second[2]["count"] == 1


def test_auto_negotiation_does_not_claim_modern_server_has_legacy_session_state():
    binding = stateful_binding()
    binding["config"].pop("protocol_mode")
    with MCPSessionManager() as manager:
        first = manager.run(counter, manager, "chat", binding)
        second = manager.run(counter, manager, "chat", binding)
        assert first[0] == second[0] and second[1]
        assert first[3] == second[3] == "2026-07-28"
        # 真实服务采用逐请求状态；保持客户端连接不能把它变成有状态协议。
        assert first[2]["count"] == second[2]["count"] == 1
        assert first[2]["session"] != second[2]["session"]
