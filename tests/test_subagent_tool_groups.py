"""角色工具组只批量选择现有只读能力；旧绑定和保存过的展开结果保持稳定。"""

import importlib

import pytest
from test_api_worker import client as _client
from test_api_worker import services as _services
from test_subagent_api import definition

client = _client
services = _services


def test_role_group_expands_without_replacing_explicit_tools_and_shrink_revokes(client, services):
    from knowledgeops_agent.harness.subagents.registry import SubagentRegistry

    response = client.post(
        "/api/subagents",
        json=definition(tools=["read_tool_result"], tool_groups=["materials_read"], enabled=True),
    )
    assert response.status_code == 201
    first = response.json()
    assert first["tools"] == ["read_tool_result"]
    assert set(first["resolved_tools"]) == {
        "read_tool_result",
        "list_materials",
        "list_evidence",
        "inspect_material",
        "read_source",
    }
    assert first["tool_groups"] == ["materials_read"]
    narrowed = client.patch(
        f"/api/subagents/{first['id']}", json={"expected_revision": 1, "tool_groups": []}
    ).json()
    assert narrowed["tools"] == narrowed["resolved_tools"] == ["read_tool_result"]
    assert narrowed["authorization_epoch"] == first["authorization_epoch"] + 1
    with pytest.raises(PermissionError, match="撤回"):
        SubagentRegistry(services.sessions).validate_binding(first)


def test_saved_group_members_do_not_expand_on_catalog_or_prompt_edit(client, services, monkeypatch):
    from knowledgeops_agent.harness.subagents.registry import SubagentRegistry

    response = client.post(
        "/api/subagents", json=definition(tools=[], tool_groups=["materials_read"], enabled=True)
    )
    assert response.status_code == 201
    first = response.json()
    catalog = importlib.import_module("knowledgeops_agent.harness.tools.catalog")
    monkeypatch.setattr(
        catalog,
        "STATIC_TOOLS",
        (
            *catalog.STATIC_TOOLS,
            catalog.ToolDefinition("new_material_tool", "后来新增", "materials_read"),
        ),
    )
    second = client.patch(
        f"/api/subagents/{first['id']}",
        json={"expected_revision": 1, "system_prompt": "仅修改角色说明。"},
    ).json()
    assert second["resolved_tools"] == first["resolved_tools"]
    assert "new_material_tool" not in second["resolved_tools"]
    assert SubagentRegistry(services.sessions).validate_binding(first) == first
    assert client.get(f"/api/subagents/{first['id']}/history").json() == [first, second]


@pytest.mark.parametrize(
    "group", ["knowledge_maintenance", "delegation", "skills", "missing"]
)
def test_role_groups_cannot_add_write_recursive_or_unknown_permissions(client, group):
    response = client.post("/api/subagents", json=definition(tool_groups=[group]))
    assert response.status_code == 422


def test_mcp_group_is_only_an_inheritance_selector_and_removal_revokes(client, services):
    from knowledgeops_agent.harness.subagents.registry import SubagentRegistry

    response = client.post("/api/subagents", json=definition(tools=[], tool_groups=["mcp"], enabled=True))
    assert response.status_code == 201
    first = response.json()
    assert first["resolved_tools"] == []
    narrowed = client.patch(f"/api/subagents/{first['id']}", json={"expected_revision": 1, "tool_groups": []})
    assert narrowed.status_code == 200
    assert narrowed.json()["authorization_epoch"] == first["authorization_epoch"] + 1
    with pytest.raises(PermissionError, match="撤回"):
        SubagentRegistry(services.sessions).validate_binding(first)


def test_role_group_null_clears_groups_but_omission_keeps_them(client):
    first = client.post(
        "/api/subagents", json=definition(tools=["read_tool_result"], tool_groups=["web"])
    ).json()
    assert "id" in first
    url = f"/api/subagents/{first['id']}"
    edited = client.patch(url, json={"expected_revision": 1, "display_name": "新展示名"}).json()
    assert edited["tool_groups"] == ["web"]
    cleared = client.patch(url, json={"expected_revision": 2, "tool_groups": None})
    assert cleared.status_code == 200
    assert cleared.json()["tool_groups"] is None
    assert cleared.json()["resolved_tools"] == ["read_tool_result"]
