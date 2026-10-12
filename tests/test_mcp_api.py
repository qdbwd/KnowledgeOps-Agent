"""用户管理接口保存配置，不默认启动连接，更不自动调用收费模型。"""

from test_api_worker import client as _client
from test_api_worker import services as _services

client = _client
services = _services


def test_mcp_api_empty_create_revision_export_and_delete(client, services):
    assert client.get("/api/mcp/connections").json() == []
    response = client.post(
        "/api/mcp/connections",
        json={
            "name": "资料服务",
            "config": {
                "transport": "http",
                "url": "http://127.0.0.1:9876/mcp",
                "header_refs": {"Authorization": "MCP_DEMO_TOKEN"},
            },
        },
    )
    assert response.status_code == 201
    row = response.json()
    url = f"/api/mcp/connections/{row['id']}"
    assert row["enabled"] is False and row["tools"] == []
    assert client.patch(url, json={"expected_revision": 9, "enabled": True}).status_code == 409
    export = client.get(url + "/export").json()
    assert export["config"]["header_refs"] == {"Authorization": "MCP_DEMO_TOKEN"}
    assert "permissions" not in export
    assert client.patch(url, json={"expected_revision": 1, "deleted": True}).status_code == 200
    assert client.get("/api/mcp/connections").json() == []
    assert len(services.gateway.ledger.records()) == 0


def test_mcp_api_refuses_inline_execution_and_embedded_credentials(client):
    for config in [
        {"transport": "stdio", "command": "sh", "args": ["-c", "echo x"]},
        {"transport": "http", "url": "https://user:secret@example.test/mcp"},
        {"transport": "http", "url": "https://example.test/mcp?access_token=hidden-credential"},
    ]:
        result = client.post("/api/mcp/connections", json={"name": "无效", "config": config})
        assert result.status_code in {409, 422}
        assert "user:secret" not in result.text
        assert "hidden-credential" not in result.text
