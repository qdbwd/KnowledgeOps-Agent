"""目录必须覆盖已有MCP来源，管理读取不触发连接、模型或权限变更。"""

from types import SimpleNamespace

import pytest
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from test_api_worker import client as _client
from test_api_worker import services as _services
from test_mcp_business import configured
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

client = _client
services = _services
runtime_services = _runtime_services


def test_enabled_mcp_is_described_in_actual_graph_without_changing_wire_name(runtime_services):
    """原MCP调用名仍可用于旧消息，同时提供不受截断影响的完整管理身份。"""
    from knowledgeops_agent.run_resources import content_hash

    module, context, _ = setup_run(runtime_services)
    row = configured(context)
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    wire_name = (
        "mcp_"
        + row["id"].replace("-", "")[:12]
        + "_get_note_"
        + content_hash({"name": "get_note"})[:8]
    )
    assert wire_name in graph.nodes["tools"].bound.tools_by_name
    descriptor = next(item for item in context.tool_catalog if item["name"] == wire_name)
    assert descriptor["id"] == f"mcp:{row['id']}:get_note"
    assert descriptor["source"] == "mcp" and descriptor["original_name"] == "get_note"
    assert descriptor["connection_id"] == row["id"]
    assert descriptor["schema"]["properties"]["topic"]["type"] == "string"
    assert "runtime" not in descriptor["schema"]["properties"]
    assert "config" not in descriptor
    assert len(context.gateway.ledger.records()) == 0


def test_catalog_api_reads_existing_mcp_state_without_connecting_or_granting(
    client, services, monkeypatch
):
    """统一入口读取原服务的最新许可，不建立另一份配置，也不因展示而激活服务。"""
    from knowledgeops_agent.mcp_registry import MCPRegistry

    row = configured(SimpleNamespace(jobs=services.jobs))
    registry = MCPRegistry(services.sessions)

    def unexpected(*args, **kwargs):
        raise AssertionError("查看工具目录不能连接MCP服务")

    monkeypatch.setattr("knowledgeops_agent.mcp_registry.connected_tools", unexpected)
    response = client.get("/api/tools/catalog")
    assert response.status_code == 200
    body = response.json()
    by_id = {item["id"]: item for item in body["tools"]}
    assert {"builtin:read_source", "middleware:read_file", "middleware:task"} <= set(by_id)
    external_id = f"mcp:{row['id']}:get_note"
    assert by_id[external_id]["connection_enabled"] is True
    assert by_id[external_id]["permission"] == "read"
    assert by_id[external_id]["unavailable_reasons"] == []
    assert "config" not in by_id[external_id]
    assert row["config"]["command"] not in response.text
    assert body["management"]["mcp"] == "/api/mcp/connections"
    disabled = registry.update(row["id"], expected_revision=row["revision"], enabled=False)
    after = {item["id"]: item for item in client.get("/api/tools/catalog").json()["tools"]}
    assert after[external_id]["connection_revision"] == disabled["revision"]
    assert after[external_id]["connection_enabled"] is False
    assert after[external_id]["unavailable_reasons"] == ["connection_disabled"]
    assert registry.get(row["id"])["revision"] == disabled["revision"]
    assert len(services.gateway.ledger.records()) == 0


def test_catalog_rejects_duplicate_and_undeclared_tools_before_graph_dispatch(runtime_services):
    """名称分派不能静默覆盖已有工具；新工具也必须有明确的目录身份。"""
    from knowledgeops_agent.tool_catalog import describe_toolset

    module, _, _ = setup_run(runtime_services)
    with pytest.raises(ValueError, match="名称冲突"):
        describe_toolset([module.read_source, module.read_source], [])

    @tool
    def undeclared_action() -> str:
        """没有登记的测试能力。"""
        return "unused"

    with pytest.raises(ValueError, match="尚未登记"):
        describe_toolset([undeclared_action], [])


def test_mcp_full_identity_survives_prefix_collision_but_graph_refuses_duplicate_names(
    runtime_services,
):
    """完整ID区分连接，旧协议的短名称若真的碰撞则拒绝装配，不能覆盖执行对象。"""
    from knowledgeops_agent.mcp_calls import mcp_tools
    from knowledgeops_agent.tool_catalog import describe_toolset, mcp_tool_options

    _, context, _ = setup_run(runtime_services)
    bindings = [
        {
            "id": identifier,
            "name": "本地测试来源",
            "revision": 1,
            "authorization_epoch": 0,
            "enabled": True,
            "tools": [
                {
                    "name": "same",
                    "description": "同名测试",
                    "schema": {"type": "object", "properties": {}},
                    "permission": "read",
                }
            ],
        }
        for identifier in [
            "12345678-9abc-4000-8000-000000000001",
            "12345678-9abc-4000-8000-000000000002",
        ]
    ]
    descriptors = mcp_tool_options(bindings)
    assert len({item["id"] for item in descriptors}) == 2
    context.resources = {"mcp": bindings}
    with pytest.raises(ValueError, match="名称冲突"):
        describe_toolset(mcp_tools(context), [], mcp_bindings=bindings)
