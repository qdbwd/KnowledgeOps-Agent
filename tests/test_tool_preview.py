"""配置预览必须与真实装配一致，且不能因查看而创建任务或连接外部服务。"""

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select
from test_api_worker import client as _client
from test_api_worker import services as _services
from test_runtime import setup_run

from knowledgeops_agent.db import Run, ToolPolicy
from knowledgeops_agent.subagent_registry import SubagentRegistry
from knowledgeops_agent.tool_policy import ToolPolicyService

client = _client
services = _services


def preview(client, **query):
    result = client.get("/api/tools/effective", params=query)
    assert result.status_code == 200
    return result.json()


def test_preview_read_is_side_effect_free_and_explains_root_options(client, services):
    result = preview(client, knowledge_only=True, auxiliary=False, delegation=False)
    entries = {row["name"]: row for row in result["tools"]}
    assert entries["read_source"]["available"]
    assert entries["read_file"]["available"]
    assert entries["search_web"]["unavailable_reasons"] == ["knowledge_only"]
    assert entries["call_model"]["unavailable_reasons"] == ["auxiliary_disabled"]
    assert "delegation_disabled" in entries["task"]["unavailable_reasons"]
    assert not entries["delegate_research"]["available"]
    assert result["policy_revision"] == 0
    with services.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 0
        assert session.scalar(select(func.count()).select_from(ToolPolicy)) == 0
    assert len(services.gateway.ledger.records()) == 0


def test_global_disable_explains_skill_dependency_and_role_selection(client, services):
    ToolPolicyService(services.sessions).update(
        expected_revision=0,
        enabled={"middleware:read_file": False},
        main_tool_groups=["materials_read", "skills"],
        main_tools=[],
    )
    result = preview(client)
    entries = {row["name"]: row for row in result["tools"]}
    assert entries["read_source"]["available"]
    assert entries["read_file"]["unavailable_reasons"] == ["globally_disabled"]
    assert "main_not_selected" in entries["publish_changes"]["unavailable_reasons"]
    assert result["skills"]["available"] is False
    assert "read_file" in result["skills"]["explanation"]
    assert result["policy_revision"] == 1


@pytest.mark.parametrize(
    "knowledge_only,auxiliary,delegation", [(False, False, True), (True, True, False)]
)
def test_preview_matches_real_root_graph(client, services, knowledge_only, auxiliary, delegation):
    ToolPolicyService(services.sessions).update(
        expected_revision=0,
        enabled={"builtin:list_materials": False},
    )
    module, context, run = setup_run(
        (services.jobs, services.knowledge, services.sources, services.gateway.ledger),
        auxiliary=auxiliary,
    )
    with services.sessions.begin() as session:
        row = session.get(Run, run["id"])
        row.input_data = {
            **row.input_data,
            "knowledge_only": knowledge_only,
            "delegation": delegation,
        }
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    result = preview(
        client, knowledge_only=knowledge_only, auxiliary=auxiliary, delegation=delegation
    )
    assert {row["name"] for row in result["tools"] if row["available"]} == set(
        graph.nodes["tools"].bound.tools_by_name
    )


def test_child_groups_ignore_main_selection_but_respect_global_and_task_limits(client, services):
    role = SubagentRegistry(services.sessions).create(
        name="preview-reader",
        display_name="预览阅读者",
        description="预览角色",
        system_prompt="只读材料",
        tools=[],
        tool_groups=["materials_read", "web"],
        skills=[],
        enabled=True,
    )
    ToolPolicyService(services.sessions).update(
        expected_revision=0,
        main_tools=["middleware:task"],
        enabled={"builtin:inspect_material": False},
    )
    result = preview(client, role_id=role["id"], knowledge_only=True)
    assert {row["name"] for row in result["tools"] if row["available"]} == {
        "list_materials",
        "read_source",
    }
    assert result["role"]["revision"] == role["revision"]
    SubagentRegistry(services.sessions).update(role["id"], expected_revision=1, enabled=False)
    assert not any(row["available"] for row in preview(client, role_id=role["id"])["tools"])
    assert client.get("/api/tools/effective", params={"role_id": "missing"}).status_code == 404


def test_preview_mcp_uses_original_permission_without_connecting(client, services, monkeypatch):
    from knowledgeops_agent.mcp_registry import MCPRegistry

    registry = MCPRegistry(services.sessions)
    connection = registry.create(
        "预览服务", {"transport": "stdio", "command": "python", "args": ["existing.py"]}
    )
    registry.update(
        connection["id"],
        expected_revision=1,
        enabled=True,
        discovered=[
            {
                "name": "inspect",
                "description": "外部读取",
                "schema": {"type": "object", "properties": {}},
                "schema_hash": "fixed-fixture",
                "annotations": {},
            }
        ],
    )

    def no_discovery(*args, **kwargs):
        raise AssertionError("查看预览不能连接服务")

    monkeypatch.setattr(MCPRegistry, "discover", no_discovery)
    result = preview(client)
    entry = next(row for row in result["tools"] if row["source"] == "mcp")
    assert not entry["available"] and "permission_denied" in entry["unavailable_reasons"]
    assert "config" not in entry
    registry.update(connection["id"], expected_revision=2, permissions={"inspect": "confirm"})
    entry = next(row for row in preview(client)["tools"] if row["source"] == "mcp")
    assert entry["available"] and entry["requires_approval"]
    assert not next(
        row for row in preview(client, knowledge_only=True)["tools"] if row["source"] == "mcp"
    )["available"]
