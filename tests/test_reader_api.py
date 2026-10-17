"""阅读器只开放工作区交付文件；材料删除不会损坏固定版本。"""

from test_api_worker import client as _client
from test_api_worker import services as _services

client = _client
services = _services


def test_workspace_preview_keeps_delivery_boundary(client, services, tmp_path):
    root = tmp_path / 'mounted'
    outputs = root / 'outputs'
    outputs.mkdir(parents=True)
    (outputs / 'note.md').write_text('# 全文\n\n尾段。', encoding='utf-8')
    (outputs / 'binary.dat').write_bytes(b'\0\1')
    (root / 'private.txt').write_text('private', encoding='utf-8')
    (outputs / 'escape.txt').symlink_to(root / 'private.txt')
    services.settings.workspace_root = root
    assert client.get('/api/workspace/preview', params={'path': 'outputs/note.md'}).json()['text'] == '# 全文\n\n尾段。'
    assert client.get('/api/workspace/preview', params={'path': 'outputs/binary.dat'}).json()['text'] is None
    for path in ['private.txt', 'outputs/../private.txt', 'outputs/escape.txt', '/etc/passwd']:
        assert client.get('/api/workspace/preview', params={'path': path}).status_code in {403, 404, 409, 422}


def test_source_full_content_and_idempotent_delete_http(client):
    body = '# 文档\n\n' + '原文' * 5000 + '\n\n最后一段'
    result = client.post('/api/sources/text', json={'title': '全文HTTP验收', 'content': body})
    assert result.status_code == 201
    source = result.json()
    content_path = f"/api/source-versions/{source['version_id']}/content"
    assert client.get(content_path).json()['text'] == body
    assert client.delete(f"/api/sources/{source['id']}").status_code == 200
    assert client.delete(f"/api/sources/{source['id']}").status_code == 200
    assert source['id'] not in [item['id'] for item in client.get('/api/sources').json()]
    assert client.get(content_path).json()['text'] == body
