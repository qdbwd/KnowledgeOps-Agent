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


def test_source_full_content_and_idempotent_delete_http(client, services):
    body = '# 文档\n\n' + '原文' * 5000 + '\n\n最后一段'
    result = client.post('/api/sources/text', json={'title': '全文HTTP验收', 'content': body})
    assert result.status_code == 201
    source = result.json()
    content_path = f"/api/source-versions/{source['version_id']}/content"
    assert client.get(content_path).json()['text'] == body
    # 移除材料后保留的是实际被独立资产引用的版本，不再无条件保留所有旧正文。
    reference = client.post('/api/sources/text', json={
        'title': '引用固定正文', 'content': f"[全文](/source-versions/{source['version_id']})",
    })
    assert reference.status_code == 201
    for _ in range(100):
        if services.lifecycle.retained.catalog.step(limit=100)['complete']:
            break
    else:
        raise AssertionError('reference catalog did not finish')
    preview = client.get(f"/api/assets/source/{source['id']}/deletion-preview").json()
    assert preview['eligible'] and preview['counts'] == {'delete': 0, 'keep': 1}
    path = f"/api/sources/{source['id']}"
    assert client.delete(path).status_code == 422
    accepted = client.request('DELETE', path, json={'expected_version': preview['version']})
    assert accepted.status_code == 202
    replay = client.request('DELETE', path, json={'expected_version': preview['version']})
    assert replay.status_code == 202 and replay.json()['id'] == accepted.json()['id']
    for _ in range(10):
        services.lifecycle.advance(accepted.json()['id'])
        state = client.get(f"/api/asset-deletions/{accepted.json()['id']}").json()
        if state['status'] == 'completed':
            break
        assert state['status'] != 'failed', state
    assert state['status'] == 'completed' and state['summary']['kept'] == 1
    assert source['id'] not in [item['id'] for item in client.get('/api/sources').json()]
    assert client.get(content_path).json()['text'] == body
