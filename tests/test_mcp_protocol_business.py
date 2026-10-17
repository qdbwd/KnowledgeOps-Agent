"""MCP业务薄层跨真实HTTP协议与远端Schema变化；不访问公共网络。"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.harness.mcp.calls import MCPCalls
from knowledgeops_agent.harness.mcp.registry import MCPRegistry
from knowledgeops_agent.harness.mcp.transport import child_environment
from knowledgeops_agent.harness.runtime.resources import prepare_resources

runtime_services = _runtime_services


def register(context, config):
    registry = MCPRegistry(context.jobs.sessions)
    row = registry.create("协议业务验证", config)
    row = registry.discover(row["id"], expected_revision=1)
    registry.update(
        row["id"], expected_revision=row["revision"], enabled=True, permissions={"get_note": "read"}
    )
    prepare_resources(context)
    return context.resources["mcp"][0]


def test_remote_schema_change_stops_before_external_tool_invocation(runtime_services, monkeypatch):
    _, context, _ = setup_run(runtime_services)
    monkeypatch.setenv("MCP_PROBE_SCHEMA", "v1")
    binding = register(
        context,
        {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(Path("tests/fixtures/mcp_probe.py").resolve())],
            "environment_refs": {"MCP_PROBE_SCHEMA": "MCP_PROBE_SCHEMA"},
        },
    )
    monkeypatch.setenv("MCP_PROBE_SCHEMA", "v2")
    result = MCPCalls(context).invoke(
        binding, "get_note", {"topic": "变化"}, call_id="schema-change"
    )
    assert result["status"] == "failed"
    assert "未执行" in result["error"]
    assert not result.get("source_version_id")


def test_http_discovery_permission_call_and_source_snapshot(runtime_services, tmp_path):
    _, context, _ = setup_run(runtime_services)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (tmp_path / "http-service.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path("tests/fixtures/mcp_probe.py").resolve()),
                "--http-port",
                str(port),
            ],
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
                    time.sleep(0.25)
            else:
                raise AssertionError("本地HTTP服务未在60秒内启动")
            binding = register(
                context, {"transport": "http", "url": f"http://127.0.0.1:{port}/mcp"}
            )
            result = MCPCalls(context).invoke(
                binding, "get_note", {"topic": "HTTP材料"}, call_id="http-evidence"
            )
            assert result["status"] == "succeeded"
            evidence = context.sources.read_segment(
                context.sources.get_version(result["source_version_id"])["segments"][0]["id"],
                root_id=context.root_id,
            )
            assert evidence["citation"] and "HTTP材料" in evidence["text"]
            configured_key = os.getenv("OPENAI_API_KEY", "")
            leaked = bool(configured_key and configured_key in evidence["text"])
            assert not leaked, "MCP返回材料中不得出现应用模型凭据"
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
