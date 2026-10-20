"""扩展备份必须保留包/锁与历史，但不能带入秘密、执行环境或自动启用。"""

import hashlib
import io
import os
import tarfile

import pytest

from knowledgeops_agent.harness.mcp.management import MCPManagement
from knowledgeops_agent.harness.mcp.registry import MCPRegistry, configuration
from knowledgeops_agent.persistence import models as db
from knowledgeops_agent.persistence.extensions import create_extensions, restore_extensions


def fixture_install(sessions, root, *, real=False, manager='uv'):
    management = MCPManagement(sessions, root=root)
    raw = {'name': 'portable', 'command': 'uvx' if manager == 'uv' else 'npx',
           'args': ['mcp-server-time' if manager == 'uv' else '@modelcontextprotocol/server-memory']}
    item = management.import_config(raw)[0]
    queued = management.prepare(item['id'])
    identity = queued['result']['installation_id']
    if real:
        result = management.execute(item['id'], queued['attempts'])
        assert result['status'] == 'prepared', result['error_code']
        with sessions() as session:
            config = session.get(db.ExtensionOperation, item['id']).result['config']
    else:
        directory = management.storage.create(item['target'], identity, item['id'])
        locks = {'requirements.in': 'example==1\n', 'requirements.lock': 'example==1 --hash=sha256:' + 'a' * 64}
        for name, content in locks.items():
            (directory / name).write_text(content)
        (directory / 'server').write_text('#!/bin/sh\nexit 0\n')
        (directory / 'server').chmod(0o755)
        manifest = management.storage.complete(identity, {'request': item['parameters']['installation'],
            'entrypoint': 'server', 'lock_files': {key: hashlib.sha256(value.encode()).hexdigest() for key, value in locks.items()}})
        config = configuration({'transport': 'stdio', 'command': str(directory / 'server'), 'installation_id': identity,
                                'cwd': str(management.storage.service_directory('data', item['target']))})
        with sessions.begin() as session:
            record = session.get(db.MCPInstallation, identity)
            record.status, record.manifest = 'prepared', manifest
    with sessions.begin() as session:
        row = db.MCPConnection(id=item['target'], name='portable', config=config, enabled=True)
        session.add(row)
        MCPRegistry._save(session, row)
        session.get(db.ExtensionOperation, item['id']).status = 'active'
    return management, identity, item['target']


@pytest.mark.parametrize('include_data', [False, True])
def test_backup_restore_is_portable_without_secrets_or_implicit_execution(business_sessions, tmp_path, monkeypatch, include_data):
    manager, identity, connection = fixture_install(business_sessions, tmp_path / 'original-mcp')
    manager.secrets.update({'PORTABLE_SECRET': 'do-not-back-up-this-value'})
    data = manager.storage.service_directory('data', connection)
    (data / 'result.json').write_text('{"saved":true}')
    (manager.storage.service_directory('cache', connection) / 'cached').write_text('cache-not-needed')
    skills = tmp_path / 'skills'
    (skills / 'scripted' / 'scripts').mkdir(parents=True)
    (skills / 'scripted' / 'SKILL.md').write_text('---\nname: scripted\ndescription: example\n---\nRead scripts/example.py')
    (skills / 'scripted' / 'scripts' / 'example.py').write_text('print("example")')
    (skills / 'scripted' / 'asset.bin').write_bytes(bytes(range(256)))
    archive = tmp_path / 'extensions.tar.gz'
    create_extensions(business_sessions, skills=skills, mcp=manager.storage.root, output=archive, include_data=include_data)
    with tarfile.open(archive) as packed:
        names = packed.getnames()
        assert not any('/cache/' in name or name.endswith('/.env') or name.endswith('/server') for name in names)
        assert b'do-not-back-up-this-value' not in b''.join(packed.extractfile(name).read() for name in names)
    restored_root = tmp_path / 'different-machine'
    result = restore_extensions(business_sessions, archive=archive, target=restored_root)
    assert not result['dependencies_installed'] and not result['secrets_restored']
    assert (restored_root / 'skills/scripted/asset.bin').read_bytes() == bytes(range(256))
    restored = MCPManagement(business_sessions, root=restored_root / 'mcp')
    assert not (restored.storage.release(identity) / 'server').exists()
    assert (restored.storage.checked('data', connection) / 'result.json').exists() == include_data
    assert not restored.secrets.project_values()
    with business_sessions() as session:
        current = session.get(db.MCPConnection, connection)
        assert not current.enabled and current.config['installation_id'] == identity
        assert session.get(db.MCPConnectionRevision, (connection, 1)).content['enabled']
        assert session.get(db.MCPConnectionRevision, (connection, 2)).content['config']['installation_id'] == identity
        assert session.get(db.MCPInstallation, identity).status == 'restore_required'
    candidate = next(item for item in restored.list() if item['parameters'].get('restore_from') == identity)
    assert candidate['parameters']['config']['cwd'] is None
    with pytest.raises(ValueError, match='尚未就绪'):
        MCPRegistry(business_sessions).update(connection, expected_revision=2, enabled=True)
    locks_received = []

    def receive(connection, installation, operation, request, **kwargs):
        locks_received.append(kwargs['lock_files'])
        raise ValueError('stop before execution')

    monkeypatch.setattr(restored.preparer, 'prepare', receive)
    queued = restored.prepare(candidate['id'])
    assert not locks_received
    restored.execute(candidate['id'], queued['attempts'])
    assert locks_received == [manager.storage.portable(identity)[1]]
    # 失败后按相同固定锁再试，不能退回解析latest；普通导入仍无restore_from。
    assert restored.get(candidate['id'])['parameters']['restore_from'] == identity


def test_extension_restore_refuses_hash_changes_links_and_existing_directory(business_sessions, tmp_path):
    skills = tmp_path / 'skills'
    skills.mkdir()
    (skills / 'SKILL.md').write_text('example')
    archive = tmp_path / 'extensions.tar.gz'
    create_extensions(business_sessions, skills=skills, mcp=tmp_path / 'mcp', output=archive)
    with tarfile.open(archive) as packed:
        contents = {name: packed.extractfile(name).read() for name in packed.getnames()}
    contents['skills/SKILL.md'] = b'altered'
    bad = tmp_path / 'altered.tar.gz'
    with tarfile.open(bad, 'w:gz') as packed:
        for name, data in contents.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            packed.addfile(info, io.BytesIO(data))
    target = tmp_path / 'uncreated'
    with pytest.raises(ValueError, match='摘要'):
        restore_extensions(business_sessions, archive=bad, target=target)
    assert not target.exists()
    with tarfile.open(bad, 'w:gz') as packed:
        info = tarfile.TarInfo('extensions.json')
        info.type, info.linkname = tarfile.SYMTYPE, '/etc/passwd'
        packed.addfile(info)
    with pytest.raises(ValueError, match='链接'):
        restore_extensions(business_sessions, archive=bad, target=target)
    target.mkdir()
    with pytest.raises(ValueError, match='新目录'):
        restore_extensions(business_sessions, archive=archive, target=target)


@pytest.mark.skipif(os.environ.get('VERIFY_MCP_PACKAGE_NETWORK') != '1', reason='explicit package restore verification')
@pytest.mark.parametrize('package_manager', ['npm', 'uv'])
def test_real_restored_lock_rebuild_and_protocol(business_sessions, tmp_path, monkeypatch, package_manager):
    import asyncio

    from knowledgeops_agent.harness.mcp.transport import MCPConnectionConfig, connected_tools

    manager, identity, connection = fixture_install(business_sessions, tmp_path / 'original', real=True, manager=package_manager)
    original_locks = manager.storage.portable(identity)[1]
    archive = tmp_path / 'extensions.tar.gz'
    create_extensions(business_sessions, skills=tmp_path / 'empty-skills', mcp=manager.storage.root, output=archive)
    target = tmp_path / 'new-machine'
    restore_extensions(business_sessions, archive=archive, target=target)
    monkeypatch.setenv('MCP_DIRECTORY', str(target / 'mcp'))
    restored = MCPManagement(business_sessions, root=target / 'mcp')
    candidate = next(item for item in restored.list() if item['parameters'].get('restore_from') == identity)
    queued = restored.prepare(candidate['id'])
    ready = restored.execute(candidate['id'], queued['attempts'])
    assert ready['status'] == 'prepared', ready['error_code']
    assert restored.storage.portable(ready['result']['installation_id'])[1] == original_locks
    assert not any(item['phase'] == 'resolve' for item in ready['result']['progress'])
    tested = restored.test(candidate['id'])
    assert tested['status'] == 'tested', tested['error_code']
    row = restored.activate(candidate['id'], permissions={tool['name']: 'read' for tool in tested['result']['tools']}, enabled=True)

    async def invoke():
        async with connected_tools(MCPConnectionConfig.model_validate(row['config']), secrets={}) as tools:
            name, arguments = ('read_graph', {}) if package_manager == 'npm' else ('get_current_time', {'timezone': 'UTC'})
            tool = next(tool for tool in tools if tool.name == name)
            answer = await tool.ainvoke({'name': name, 'args': arguments, 'id': 'restored-call', 'type': 'tool_call'})
            assert answer.status == 'success'

    asyncio.run(invoke())
