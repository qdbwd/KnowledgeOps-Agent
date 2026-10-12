"""工具配置的持久化合同：读不写、并发修订、空选择和收窄后的永久撤回。"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select
from test_api_worker import client as _client
from test_api_worker import services as _services

client = _client
services = _services


def test_default_policy_read_does_not_create_configuration_or_grant_calls(client, services):
    response = client.get("/api/tools/policy")
    assert response.status_code == 200
    policy = response.json()
    assert policy["revision"] == 0
    assert policy["main_tools"] is None and policy["main_tool_groups"] is None
    assert policy["resolved_main_tools"] is None
    assert policy["enabled"]["middleware:read_file"] is True
    assert policy["enabled"]["middleware:task"] is True
    from knowledgeops_agent.db import ToolPolicy, ToolPolicyRevision

    with services.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ToolPolicy)) == 0
        assert session.scalar(select(func.count()).select_from(ToolPolicyRevision)) == 0
    assert len(services.gateway.ledger.records()) == 0


def test_policy_revisions_conflict_and_disable_enable_do_not_revive_old_binding(client, services):
    from knowledgeops_agent.tool_policy import ToolPolicyService

    service = ToolPolicyService(services.sessions)
    original = service.get()
    disabled = client.patch(
        "/api/tools/policy", json={"expected_revision": 0, "enabled": {"builtin:read_web": False}}
    )
    assert disabled.status_code == 200
    first = disabled.json()
    assert first["revision"] == 1 and first["tool_epochs"]["builtin:read_web"] == 1
    assert (
        client.patch(
            "/api/tools/policy",
            json={"expected_revision": 0, "enabled": {"builtin:read_web": True}},
        ).status_code
        == 409
    )
    enabled = client.patch(
        "/api/tools/policy", json={"expected_revision": 1, "enabled": {"builtin:read_web": True}}
    ).json()
    assert enabled["revision"] == 2 and enabled["tool_epochs"]["builtin:read_web"] == 1
    assert "builtin:read_web" not in service.allowed_ids(original, is_root=True)
    assert "builtin:read_source" in service.allowed_ids(original, is_root=True)
    assert "builtin:read_web" in service.allowed_ids(enabled, is_root=True)
    assert [row["revision"] for row in client.get("/api/tools/policy/history").json()] == [0, 1, 2]


def test_main_group_null_empty_and_explicit_selection_have_distinct_meaning(client):
    empty = client.patch("/api/tools/policy", json={"expected_revision": 0, "main_tool_groups": []})
    assert empty.status_code == 200
    assert empty.json()["resolved_main_tools"] == []
    grouped = client.patch(
        "/api/tools/policy",
        json={
            "expected_revision": 1,
            "main_tool_groups": ["materials_read"],
            "main_tools": ["builtin:read_tool_result"],
        },
    ).json()
    assert set(grouped["resolved_main_tools"]) == {
        "builtin:list_materials",
        "builtin:inspect_material",
        "builtin:read_source",
        "builtin:read_tool_result",
    }
    # null恢复默认继承；省略字段则保留原选择，不能混同。
    reset = client.patch(
        "/api/tools/policy",
        json={"expected_revision": 2, "main_tool_groups": None, "main_tools": None},
    ).json()
    assert reset["resolved_main_tools"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": {"mcp:connection:tool": False}},
        {"enabled": {"builtin:missing": False}},
        {"enabled": {"builtin:read_source": "false"}},
        {"main_tool_groups": ["missing"]},
        {"main_tool_groups": ["mcp"]},
        {"main_tools": ["publish_changes"]},
        {"enabled": None},
        {"tool_epochs": {"builtin:read_source": 0}},
    ],
)
def test_policy_rejects_unknown_or_server_owned_fields(client, changes):
    response = client.patch("/api/tools/policy", json={"expected_revision": 0, **changes})
    assert response.status_code == 422
    assert client.get("/api/tools/policy").json()["revision"] == 0


def test_policy_first_write_race_has_one_winner_and_preserves_history(business_sessions):
    from knowledgeops_agent.tool_policy import ToolPolicyService

    def change(name):
        try:
            return ToolPolicyService(business_sessions).update(
                expected_revision=0, enabled={name: False}
            )
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(change, ["builtin:read_web", "builtin:search_web"]))
    assert sum(item is not None for item in results) == 1
    service = ToolPolicyService(business_sessions)
    assert [row["revision"] for row in service.history()] == [0, 1]
