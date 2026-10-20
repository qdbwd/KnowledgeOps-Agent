"""候选配置与活动连接分开；准备投递不是自动授权，失败保留旧版。"""

import json
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from langchain_core.tools import StructuredTool

from knowledgeops_agent.harness.mcp.management import MCPManagement
from knowledgeops_agent.harness.mcp.registry import MCPRegistry
from knowledgeops_agent.persistence.models import ExtensionOperation, MCPInstallation, utcnow


@pytest.fixture
def manager(business_sessions, tmp_path, monkeypatch):
    monkeypatch.setenv('MCP_DIRECTORY', str(tmp_path))
    return MCPManagement(business_sessions, root=tmp_path)


@asynccontextmanager
async def discovered(*args, **kwargs):
    yield [StructuredTool.from_function(lambda topic: topic, name='lookup', description='read a topic')]


def test_import_does_not_install_or_enable_and_keeps_values_out_of_database(manager):
    preview = manager.preview({'mcpServers': {'memory': {'command': 'npx', 'args': ['-y', 'server@1'], 'env': {'KEY': 'private-value'}}}})
    assert 'private-value' not in json.dumps(preview)
    operations = manager.import_config({'mcpServers': {'memory': {'command': 'npx', 'args': ['-y', 'server@1'], 'env': {'KEY': 'private-value'}}}})
    row = operations[0]
    assert row['status'] == 'imported'
    assert MCPRegistry(manager.sessions).list() == []
    with manager.sessions() as session:
        stored = session.get(ExtensionOperation, row['id'])
        assert 'private-value' not in json.dumps([stored.parameters, stored.result])
        assert session.query(MCPInstallation).count() == 0
    assert not (manager.storage.root / 'releases').exists()
    assert 'private-value' in (manager.storage.root / '.env').read_text()


@pytest.mark.parametrize('location', ['command', 'cwd', 'argument', 'relative_argument', 'linked_argument'])
def test_import_cannot_borrow_another_managed_installation_without_its_lease(manager, tmp_path, location):
    """裸路径不能借用另一连接的安装；否则清理拥有者会损坏仍在使用的别名连接。"""
    release = manager.storage.root / 'releases' / 'other-service'
    release.mkdir(parents=True)
    entry = release / 'server.py'
    entry.write_text('print("not executed")')
    config = {'command': 'python', 'args': []}
    if location == 'command':
        config['command'] = str(entry)
    elif location == 'cwd':
        config['cwd'] = str(release)
    elif location == 'relative_argument':
        config.update(cwd=str(tmp_path.parent), args=[str(entry.relative_to(tmp_path.parent))])
    elif location == 'linked_argument':
        link = tmp_path.parent / (tmp_path.name + '-linked-server.py')
        link.symlink_to(entry)
        config['args'] = [str(link)]
    else:
        config['args'] = ['--server=' + str(entry)]
    with pytest.raises(ValueError, match='受管目录'):
        manager.import_config({'name': 'borrowed', **config})
    with pytest.raises(ValueError, match='受管目录'):
        manager.preview({'name': 'borrowed', **config})
    import asyncio

    from knowledgeops_agent.harness.mcp.transport import MCPConnectionConfig, connected_tools

    async def connect_old_config():
        async with connected_tools(MCPConnectionConfig(transport='stdio', **config), secrets={}):
            pytest.fail('借用受管安装的旧配置不能执行')

    with pytest.raises(ValueError, match='受管目录'):
        asyncio.run(connect_old_config())
    with manager.sessions() as session:
        assert session.query(ExtensionOperation).count() == 0
    assert entry.exists()


def test_candidate_failure_keeps_current_and_activation_requires_a_fresh_test(manager, monkeypatch):
    registry = MCPRegistry(manager.sessions)
    old = registry.create('active', {'transport': 'http', 'url': 'http://localhost:9999/mcp'})
    old = registry.update(old['id'], expected_revision=old['revision'], enabled=True)
    candidate = manager.import_config({'name': 'updated', 'url': 'http://localhost:9998/mcp'},
                                     connection_id=old['id'], expected_revision=old['revision'])[0]
    with pytest.raises(ValueError, match='测试'):
        manager.activate(candidate['id'], permissions={'lookup': 'read'}, enabled=True)

    @asynccontextmanager
    async def broken(*args, **kwargs):
        raise RuntimeError('private-server-output')
        yield

    monkeypatch.setattr('knowledgeops_agent.harness.mcp.management.connected_tools', broken)
    assert manager.test(candidate['id'])['status'] == 'failed'
    assert registry.get(old['id']) == old
    assert 'private-server-output' not in json.dumps(manager.get(candidate['id']))
    monkeypatch.setattr('knowledgeops_agent.harness.mcp.management.connected_tools', discovered)
    assert manager.test(candidate['id'])['status'] == 'tested'
    new = manager.activate(candidate['id'], permissions={'lookup': 'read'}, enabled=True)
    assert new['revision'] == old['revision'] + 1
    assert new['authorization_epoch'] == old['authorization_epoch'] + 1
    assert new['config']['url'] == 'http://localhost:9998/mcp'
    assert not registry.is_binding_active(old)


def test_preparation_duplicate_delivery_cancel_and_expired_lease_require_explicit_retry(manager):
    item = manager.import_config({'name': 'local', 'command': 'uvx', 'args': ['server']})[0]
    queued = manager.prepare(item['id'])
    assert queued['status'] == 'queued'
    with pytest.raises(ValueError):
        manager.prepare(item['id'])
    assert manager.execute(item['id'], queued['attempts'] + 1) is None
    manager.cancel(item['id'])
    assert manager.execute(item['id'], queued['attempts']) is None
    queued = manager.prepare(item['id'])
    with manager.sessions.begin() as session:
        row = session.get(ExtensionOperation, item['id'])
        row.status = 'preparing'
        row.lease_owner = 'lost-worker'
        row.lease_until = utcnow() - timedelta(seconds=1)
    assert manager.recover_expired() == 1
    assert manager.get(item['id'])['status'] == 'interrupted'
    assert manager.execute(item['id'], queued['attempts']) is None
    assert manager.prepare(item['id'])['attempts'] == queued['attempts'] + 1


def test_secret_change_or_revision_change_after_test_requires_revalidation(manager, monkeypatch):
    monkeypatch.setattr('knowledgeops_agent.harness.mcp.management.connected_tools', discovered)
    item = manager.import_config({'name': 'http', 'url': 'http://localhost/mcp', 'headers': {'X-Key': 'first-value'}})[0]
    manager.test(item['id'])
    reference = next(iter(item['parameters']['config']['header_refs'].values()))
    manager.secrets.update({reference: 'rotated-value'})
    with pytest.raises(ValueError, match='凭据'):
        manager.activate(item['id'], permissions={'lookup': 'read'}, enabled=True)
    manager.test(item['id'])
    active = manager.activate(item['id'], permissions={'lookup': 'read'}, enabled=True)
    update = manager.import_config({'name': 'changed', 'url': 'http://localhost/new'},
                                   connection_id=active['id'], expected_revision=active['revision'])[0]
    manager.test(update['id'])
    MCPRegistry(manager.sessions).update(active['id'], expected_revision=active['revision'], enabled=False)
    with pytest.raises(ValueError, match='修订'):
        manager.activate(update['id'], permissions={'lookup': 'read'}, enabled=True)


def test_real_redis_delivery_keeps_only_one_installation_execution(manager):
    import os
    from threading import Event
    from uuid import uuid4

    from celery.contrib.testing.worker import start_worker

    from knowledgeops_agent.app.mcp_operations import MCPPreparationPublisher
    from knowledgeops_agent.app.runs.queue import MCP_PREPARE_TASK, create_queue

    executed, deliveries = [], []
    first, second = Event(), Event()

    class LocalPreparer:
        def prepare(self, connection, installation, operation, request, **options):
            executed.append(installation)
            directory = manager.storage.create(connection, installation, operation)
            (directory / 'server').write_text('#!/bin/sh\nexit 0\n')
            (directory / 'server').chmod(0o755)
            manifest = manager.storage.complete(installation, {'request': request, 'entrypoint': 'server', 'lock_files': {}})
            return {**manifest, 'command': str(directory / 'server')}

    manager.preparer = LocalPreparer()

    def handle(identity, generation):
        deliveries.append(identity)
        manager.execute(identity, generation)
        (first if len(deliveries) == 1 else second).set()

    row = manager.import_config({'name': 'queue', 'command': 'uvx', 'args': ['server']})[0]
    queued = manager.prepare(row['id'])
    app = create_queue(os.environ['TEST_REDIS_URL'], queue_name='mcp-test-' + uuid4().hex, mcp_handler=handle)
    try:
        with start_worker(app, pool='solo', concurrency=1, perform_ping_check=False, shutdown_timeout=10):
            assert MCPPreparationPublisher(manager.sessions, app).publish_due() == 1
            assert first.wait(10)
            assert manager.get(row['id'])['status'] == 'prepared'
            app.send_task(MCP_PREPARE_TASK, args=[row['id'], queued['attempts']])
            assert second.wait(10)
    finally:
        app.close()
    assert len(executed) == 1 and len(deliveries) == 2


def test_failed_broker_keeps_preparation_queued_for_redelivery(manager):
    from knowledgeops_agent.app.mcp_operations import MCPPreparationPublisher

    class Offline:
        def send_task(self, *args, **kwargs):
            raise ConnectionError('secret-redis-url')

    item = manager.import_config({'name': 'queue', 'command': 'npx', 'args': ['server']})[0]
    manager.prepare(item['id'])
    assert MCPPreparationPublisher(manager.sessions, Offline()).publish_due() == 0
    row = manager.get(item['id'])
    assert row['status'] == 'queued' and row['error_code'] == 'dispatch_failed'
    assert 'secret-redis-url' not in json.dumps(row)
