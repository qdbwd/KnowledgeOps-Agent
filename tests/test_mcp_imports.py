"""常见启动格式先转为准备计划，不启动进程、下载依赖或持久保存秘密。"""

import json

import pytest

from knowledgeops_agent.harness.mcp.imports import parse_import


@pytest.mark.parametrize("args,packages,entry,arguments", [
    (["-y", "@example/server@1.2.3", "--port", "1234"], ["@example/server@1.2.3"], None, ["--port", "1234"]),
    (["--package", "@example/server@latest", "server", "--flag"], ["@example/server@latest"], "server", ["--flag"]),
    (["-y", "--package=one@2", "--package=two@3", "--", "serve"], ["one@2", "two@3"], "serve", []),
])
def test_npx_arguments_become_an_explicit_installation_request(args, packages, entry, arguments):
    parsed = parse_import({"mcpServers": {"example": {"command": "npx", "args": args}}})[0]
    assert parsed.installation == {"manager": "npm", "packages": packages, "entrypoint": entry, "python": None}
    assert parsed.config["args"] == arguments
    assert parsed.config["command"] is None  # 安装成功前不存在可运行入口。
    assert parsed.safe_view()["preparation_required"] is True


@pytest.mark.parametrize("args,packages,entry,python", [
    (["mcp-server-time@1.2.3", "--timezone", "UTC"], ["mcp-server-time==1.2.3"], "mcp-server-time", None),
    (["--from", "mcp-server[http]>=1,<2", "--with", "httpx==0.28.1", "serve"], ["mcp-server[http]>=1,<2", "httpx==0.28.1"], "serve", None),
    (["--python=3.12", "--from=mcp-server==1.0", "serve"], ["mcp-server==1.0"], "serve", "3.12"),
])
def test_uvx_package_command_and_python_are_separate(args, packages, entry, python):
    parsed = parse_import({"name": "clock", "config": {"command": "uvx", "args": args}})[0]
    assert parsed.installation == {"manager": "uv", "packages": packages, "entrypoint": entry, "python": python}


def test_literal_secrets_leave_only_references_in_preview_and_legacy_export_is_accepted():
    parsed = parse_import({"mcpServers": {
        "remote": {"url": "https://example.test/mcp", "headers": {"Authorization": "Bearer private-value"}},
        "local": {"command": "python", "args": ["/workspace/service.py"], "cwd": "/workspace/service",
                  "env": {"TOKEN": "${env:EXTERNAL_TOKEN}", "OTHER": "second-private-value"}},
    }})
    assert all(item.installation is None for item in parsed)
    assert parsed[1].config["environment_refs"]["TOKEN"] == "EXTERNAL_TOKEN"
    assert parsed[1].config["cwd"] == "/workspace/service"
    rendered = json.dumps([item.safe_view() for item in parsed]) + repr(parsed)
    assert "private-value" not in rendered
    assert list(parsed[0].secret_values.values()) == ["Bearer private-value"]
    restored = parse_import({"name": "old", "config": parsed[1].config})[0]
    assert restored.config == parsed[1].config and not restored.secret_values


def test_http_bearer_reference_can_be_applied_without_storing_the_secret():
    parsed = parse_import({"name": "http", "url": "http://localhost/mcp",
                           "headers": {"Authorization": "Bearer ${TOKEN}"}})[0]
    assert parsed.config["header_refs"] == {"Authorization": "TOKEN"}
    assert parsed.config["header_prefixes"] == {"Authorization": "Bearer "}
    assert not parsed.secret_values


@pytest.mark.parametrize("config", [
    {"command": "npx", "args": ["-c", "secret-inline-command"]},
    {"command": "npx", "args": ["--registry=https://private.example", "server"]},
    {"command": "npx", "args": ["https://example.test/package.tgz"]},
    {"command": "uvx", "args": ["--from", "git+https://example.test/repo", "serve"]},
    {"command": "uvx", "args": ["--python", "https://example.test/python", "serve"]},
    {"command": "sh", "args": ["-c", "private-inline"]},
    {"command": "python", "args": ["service.py"], "cwd": "C:\\private\\service"},
    {"url": "https://example.test/mcp?token=private-value"},
    {"command": "python", "args": ["service.py", "--api-key", "private-value"]},
    {"url": "http://localhost/mcp", "headers": {"X-Key": "${MISSING}-private-value"}},
])
def test_unsupported_commands_do_not_fall_back_to_shell_or_leak_inputs(config):
    with pytest.raises(ValueError) as error:
        parse_import({"mcpServers": {"invalid": config}})
    assert "private-value" not in str(error.value) and "private-inline" not in str(error.value)
