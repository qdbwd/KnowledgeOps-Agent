"""子 Agent 配置的标准 HTTP 合同；保存配置不执行模型或外部连接。"""

import pytest
from test_api_worker import client as _client
from test_api_worker import services as _services

client = _client
services = _services


def definition(**changes):
    return {
        "name": "evidence-reviewer",
        "display_name": "证据核对",
        "description": "核对用户指定材料中的条件。",
        "system_prompt": "只核对已提供的材料，说明依据和缺口。",
        "tools": ["read_source", "read_tool_result"],
        "skills": ["evidence-check"],
        **changes,
    }


def test_subagent_options_and_builtin_are_read_only(client, services):
    response = client.get("/api/subagents")
    assert response.status_code == 200
    builtin = response.json()[0]
    assert builtin["name"] == "researcher" and builtin["builtin"] is True
    options = client.get("/api/subagents/options").json()
    tool_names = {row["name"] for row in options["tools"]}
    assert {"read_source", "call_model", "search_web"} <= tool_names
    assert not {"publish_changes", "delegate_research", "capture_conversation"} & tool_names
    assert "evidence-check" in {row["name"] for row in options["skills"]}
    assert all(row["description"] for row in [*options["tools"], *options["skills"]])
    url = f"/api/subagents/{builtin['id']}"
    denied = client.patch(url, json={"expected_revision": 1, "enabled": False})
    assert denied.status_code == 403
    assert "detail" in denied.json()
    assert client.get(url + "/history").json() == [builtin]
    assert services.gateway.ledger.records() == []


def test_subagent_create_patch_conflict_and_history(client, services):
    response = client.post("/api/subagents", json=definition())
    assert response.status_code == 201
    first = response.json()
    assert first["enabled"] is False and first["deleted"] is False
    assert first["revision"] == 1 and first["authorization_epoch"] == 0
    assert first["builtin"] is False
    url = f"/api/subagents/{first['id']}"
    changed = client.patch(url, json={"expected_revision": 1, "enabled": True})
    assert changed.status_code == 200
    second = changed.json()
    assert second["revision"] == 2 and second["enabled"] is True
    assert client.patch(url, json={"expected_revision": 1, "enabled": False}).status_code == 409
    assert client.get(url + "/history").json() == [first, second]
    assert client.post("/api/subagents", json=definition()).status_code == 409
    assert len(services.gateway.ledger.records()) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "researcher"},
        {"name": "Uppercase"},
        {"name": "a" * 65},
        {"display_name": " "},
        {"description": "x" * 501},
        {"system_prompt": " "},
        {"system_prompt": "x" * 8001},
        {"tools": ["publish_changes"]},
        {"tools": ["read_source", " "]},
        {"skills": ["unknown-skill"]},
        {"enabled": "true"},
        {"model": "other-provider"},
    ],
)
def test_subagent_invalid_definition_never_creates_entry(client, changes):
    result = client.post("/api/subagents", json=definition(**changes))
    assert result.status_code == 422
    assert "detail" in result.json()
    assert [row["name"] for row in client.get("/api/subagents").json()] == ["researcher"]


def test_subagent_tombstone_preserves_revisions_and_forbids_resurrection(client, services):
    response = client.post("/api/subagents", json=definition(enabled=True))
    assert response.status_code == 201
    first = response.json()
    url = f"/api/subagents/{first['id']}"
    deleted = client.patch(url, json={"expected_revision": 1, "deleted": True})
    assert deleted.status_code == 200
    tombstone = deleted.json()
    assert tombstone["deleted"] is True and tombstone["enabled"] is False
    assert tombstone["authorization_epoch"] == 1
    assert [row["name"] for row in client.get("/api/subagents").json()] == ["researcher"]
    assert client.get(url + "/history").json() == [first, tombstone]
    assert client.patch(url, json={"expected_revision": 2, "enabled": True}).status_code == 404
    assert client.get("/api/subagents/missing/history").status_code == 404
    assert services.gateway.ledger.records() == []


def test_subagent_patch_cannot_rename_or_replace_server_owned_fields(client):
    response = client.post("/api/subagents", json=definition())
    assert response.status_code == 201
    first = response.json()
    url = f"/api/subagents/{first['id']}"
    for changes in [
        {"name": "renamed"},
        {"revision": 10},
        {"authorization_epoch": 0},
        {"builtin": True},
        {"system_prompt": None},
    ]:
        assert client.patch(url, json={"expected_revision": 1, **changes}).status_code == 422
    assert len(client.get(url + "/history").json()) == 1
