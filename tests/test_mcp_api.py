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


def test_legacy_api_cannot_alias_managed_files(client, tmp_path, monkeypatch):
    monkeypatch.setenv('MCP_DIRECTORY', str(tmp_path / 'mcp'))
    path = str(tmp_path / 'mcp' / 'releases' / 'another-installation' / 'server.py')
    for config in [
        {'command': path}, {'command': 'python', 'args': [path]},
        {'command': 'python', 'cwd': str(tmp_path / 'mcp' / 'data' / 'another-service')},
    ]:
        result = client.post('/api/mcp/connections', json={'name': 'borrowed', 'config': config})
        assert result.status_code == 409
        assert '受管目录' in result.json()['detail']


def test_import_prepare_credentials_and_cancel_are_separate_http_steps(client, services, tmp_path, monkeypatch):
    monkeypatch.setenv('MCP_DIRECTORY', str(tmp_path / 'mcp'))
    config = {'mcpServers': {'clock': {'command': 'uvx', 'args': ['mcp-server-time'], 'env': {'KEY': 'never-echo-this'}}}}
    preview = client.post('/api/mcp/import-preview', json={'value': config})
    assert preview.status_code == 200 and 'never-echo-this' not in preview.text
    assert not (tmp_path / 'mcp').exists()
    imported = client.post('/api/mcp/import', json={'value': config})
    assert imported.status_code == 201 and 'never-echo-this' not in imported.text
    item = imported.json()[0]
    url = '/api/mcp/operations/' + item['id']
    assert item['status'] == 'imported'
    assert client.get('/api/mcp/connections').json() == []
    credentials = client.get(url + '/credentials')
    assert credentials.json()[0]['source'] == 'project'
    reference = credentials.json()[0]['reference']
    assert client.put(url + '/credentials', json={'values': {reference: 'rotated-secret'}}).status_code == 200
    invalid = client.put(url + '/credentials', json={'values': {reference: {'bad': 'secret-validation'}}})
    assert invalid.status_code == 422 and 'secret-validation' not in invalid.text
    assert client.post(url + '/prepare').json()['status'] == 'queued'
    assert client.post(url + '/cancel').json()['status'] == 'cancelled'
    assert len(services.gateway.ledger.records()) == 0
