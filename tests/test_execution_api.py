"""HTTP 只接受真实用户配置；沿用应用同源控制及供应商能力校验。"""

from test_api_worker import client as _client
from test_api_worker import services as _services

client = _client
services = _services


def test_runtime_settings_validate_capabilities_and_revision(client):
    conv = client.post("/api/conversations", json={}).json()["id"]
    url = f"/api/conversations/{conv}/runtime-settings"
    assert client.get(url).json()["permission_mode"] == "ask"
    assert (
        client.patch(url, json={"expected_revision": 0, "reasoning_effort": "invented"}).status_code
        == 409
    )
    assert client.get(url).json()["revision"] == 0
    assert (
        client.patch(url, json={"expected_revision": 0, "permission_mode": "auto_review"}).json()[
            "revision"
        ]
        == 1
    )
    assert (
        client.patch(url, json={"expected_revision": 0, "permission_mode": "ask"}).status_code
        == 409
    )


def test_rule_api_cannot_escape_workspace_or_silently_overwrite(client, services, tmp_path):
    services.settings.workspace_root = tmp_path
    url = "/api/execution-rules"
    body = {"argv": ["git", "status"], "match": "prefix", "effect": "allow", "cwd": str(tmp_path)}
    response = client.post(url, json=body)
    assert response.status_code == 201
    rule = response.json()
    assert rule["workspace"] == str(tmp_path)
    assert client.post(url, json={**body, "cwd": "/etc"}).status_code == 409
    assert (
        client.patch(
            f"{url}/{rule['id']}", json={"expected_revision": 1, "effect": "deny"}
        ).status_code
        == 200
    )
    assert client.delete(f"{url}/{rule['id']}?expected_revision=1").status_code == 409
    assert client.delete(f"{url}/{rule['id']}?expected_revision=2").status_code == 204
    assert not client.get(url).json()
