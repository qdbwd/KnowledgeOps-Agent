"""真实本地stdio/HTTP协议；发现和调用均经过应用适配，不启用外部服务。"""

import asyncio
import importlib
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest


def test_connection_config_does_not_store_secret_values():
    module = importlib.import_module("knowledgeops_agent.harness.mcp.transport")
    config = module.MCPConnectionConfig(
        transport="http",
        url="https://example.test/mcp",
        header_refs={"Authorization": "MCP_TEST_TOKEN"},
    )
    assert "MCP_TEST_TOKEN" in config.model_dump_json()
    with pytest.raises(ValueError, match="missing"):
        module.resolve_secret_refs(config.header_refs, {})
    with pytest.raises(ValueError):
        module.MCPConnectionConfig(transport="http", url="file:///etc/passwd")


def test_legacy_config_keeps_its_identity_and_protocol_mode_is_explicit():
    from knowledgeops_agent.harness.mcp.registry import configuration
    from knowledgeops_agent.harness.mcp.transport import MCPConnectionConfig

    old = {
        "transport": "http", "url": "http://localhost/mcp", "command": None,
        "args": [], "environment_refs": {}, "header_refs": {}, "timeout_seconds": 30.0,
    }
    assert configuration(old) == old
    assert configuration({**old, "protocol_mode": "auto"}) == old
    assert MCPConnectionConfig.model_validate(old).protocol_mode == "auto"
    assert configuration({**old, "protocol_mode": "legacy"})["protocol_mode"] == "legacy"
    with pytest.raises(ValueError):
        configuration({**old, "protocol_mode": "unrecognized"})


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_real_protocol_discovery_and_call_keep_app_secrets_out(
    transport, monkeypatch, tmp_path
):
    module = importlib.import_module("knowledgeops_agent.harness.mcp.transport")
    fixture = str(Path("tests/fixtures/mcp_probe.py").resolve())
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-a-child-process")
    process = None
    logfile = None
    try:
        if transport == "stdio":
            config = module.MCPConnectionConfig(
                transport="stdio", command=sys.executable, args=[fixture]
            )
        else:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            logfile = (tmp_path / "server.log").open("w")
            # 使用与应用适配相同的最小子进程环境，不继承本项目模型凭据。
            process = subprocess.Popen(
                [sys.executable, fixture, "--http-port", str(port)],
                env=module.child_environment({}),
                stdout=logfile,
                stderr=logfile,
            )
            for _ in range(120):
                if process.poll() is not None:
                    raise AssertionError("fixture exited before starting")
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.5)
            else:
                raise AssertionError("fixture did not listen within 60 seconds")
            config = module.MCPConnectionConfig(
                transport="http", url=f"http://127.0.0.1:{port}/mcp"
            )
        async with module.connected_tools(config, secrets={}) as tools:
            notes = {tool.name: tool for tool in tools}
            assert "get_note" in notes
            response = await notes["get_note"].ainvoke(
                {
                    "name": "get_note",
                    "args": {"topic": "evidence"},
                    "id": "probe-call",
                    "type": "tool_call",
                }
            )
            assert response.status == "success"
            assert response.artifact["structured_content"]["topic"] == "evidence"
            assert response.artifact["structured_content"]["inherited_key"] is False
            assert "保留原始来源" in json.dumps(response.content, ensure_ascii=False)
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if logfile:
            logfile.close()
