"""项目环境文件真实读写、进程间锁、轮换与部署变量优先级。"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from knowledgeops_agent.harness.mcp.secrets import MCPSecretStore


def test_environment_precedence_missing_and_rotation_across_instances(tmp_path):
    first = MCPSecretStore(tmp_path / "mcp", environment={"TOKEN": "deployment"})
    second = MCPSecretStore(tmp_path / "mcp", environment={})
    first.update({"TOKEN": "page value", "OTHER": 'quote" and $NOT_INTERPOLATED'})
    assert first.resolve(["TOKEN"]) == {"TOKEN": "deployment"}
    assert second.resolve(["TOKEN"]) == {"TOKEN": "page value"}
    assert second.resolve(["OTHER"])["OTHER"] == 'quote" and $NOT_INTERPOLATED'
    first.update({"TOKEN": "rotated"})
    assert second.resolve(["TOKEN"])["TOKEN"] == "rotated"
    assert first.status(["TOKEN", "ABSENT"]) == [
        {"reference": "ABSENT", "source": "missing", "configured": False, "project_value": False},
        {"reference": "TOKEN", "source": "environment", "configured": True, "project_value": True},
    ]
    with pytest.raises(ValueError, match="ABSENT"):
        first.resolve(["ABSENT"])


def test_atomic_updates_preserve_other_keys_and_use_private_permissions(tmp_path):
    root = tmp_path / "mcp"

    def write(index):
        MCPSecretStore(root, environment={}).update({f"VALUE_{index}": str(index)})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(8)))
    store = MCPSecretStore(root, environment={})
    assert store.resolve([f"VALUE_{index}" for index in range(8)]) == {f"VALUE_{index}": str(index) for index in range(8)}
    assert (root / ".env").stat().st_mode & 0o777 == 0o600
    store.update({"VALUE_0": None})
    assert store.status(["VALUE_0"])[0]["source"] == "missing"
    assert store.resolve(["VALUE_1"])["VALUE_1"] == "1"


def test_failed_atomic_write_keeps_previous_values(tmp_path, monkeypatch):
    store = MCPSecretStore(tmp_path / "mcp", environment={})
    store.update({"TOKEN": "old"})
    import knowledgeops_agent.harness.mcp.secrets as module

    def fail(*_):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(OSError):
        store.update({"TOKEN": "new"})
    assert store.resolve(["TOKEN"])["TOKEN"] == "old"
    assert list(store.root.glob("*.tmp")) == []


def test_secret_file_links_and_invalid_content_are_not_overwritten(tmp_path):
    root, outside = tmp_path / "mcp", tmp_path / "outside"
    root.mkdir()
    outside.write_text("TOKEN=keep\n")
    (root / ".env").symlink_to(outside)
    store = MCPSecretStore(root, environment={})
    with pytest.raises(ValueError):
        store.update({"TOKEN": "replace"})
    assert outside.read_text() == "TOKEN=keep\n"
    (root / ".env").unlink()
    invalid = "TOKEN='unterminated-private-value\n"
    (root / ".env").write_text(invalid)
    with pytest.raises(ValueError) as error:
        store.update({"OTHER": "new"})
    assert "private-value" not in str(error.value)
    assert (root / ".env").read_text() == invalid


def test_redaction_and_status_do_not_expose_either_secret_source(tmp_path):
    store = MCPSecretStore(tmp_path / "mcp", environment={"TOKEN": "deployment-value"})
    store.update({"TOKEN": "page-value", "OTHER": "second-value"})
    redacted = store.redact({"error": ["deployment-value page-value second-value"]}, ["TOKEN", "OTHER"])
    assert redacted == {"error": ["[REDACTED] [REDACTED] [REDACTED]"]}
    assert "page-value" not in repr(store.status(["TOKEN", "OTHER"]))
    assert "deployment-value" not in repr(store)


@pytest.mark.parametrize("value", ["line\nbreak", "null\x00byte", "x" * 8193])
def test_invalid_secret_is_rejected_without_echo(tmp_path, value):
    with pytest.raises(ValueError) as error:
        MCPSecretStore(tmp_path / "mcp", environment={}).update({"TOKEN": value})
    assert value not in str(error.value)


def test_two_session_managers_observe_project_rotation_and_keep_only_declared_values(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    from test_mcp_sessions import Adapter, binding, identify

    from knowledgeops_agent.harness.mcp.sessions import MCPSessionManager

    monkeypatch.setenv("MCP_DIRECTORY", str(tmp_path / "mcp"))
    monkeypatch.delenv("MCP_ROTATION_TEST_TOKEN", raising=False)
    store = MCPSecretStore()
    store.update({"MCP_ROTATION_TEST_TOKEN": "first", "UNRELATED_MCP_TOKEN": "must-not-send"})
    opened, closed = [], []

    @asynccontextmanager
    async def connect(config, *, secrets):
        assert set(secrets) == {"MCP_ROTATION_TEST_TOKEN"}
        adapter = Adapter()
        opened.append(secrets["MCP_ROTATION_TEST_TOKEN"])
        try:
            yield adapter
        finally:
            closed.append(adapter)

    config = binding()
    config["config"]["header_refs"] = {"Authorization": "MCP_ROTATION_TEST_TOKEN"}
    with MCPSessionManager(connector=connect) as one, MCPSessionManager(connector=connect) as two:
        previous = [manager.run(identify, manager, "chat", config)[0] for manager in (one, two)]
        store.update({"MCP_ROTATION_TEST_TOKEN": "second"})
        for manager in (one, two):
            manager.run(manager.sweep)
        assert len(closed) == 2
        current = [manager.run(identify, manager, "chat", config)[0] for manager in (one, two)]
        assert not set(previous) & set(current)
        assert opened == ["first", "first", "second", "second"]
