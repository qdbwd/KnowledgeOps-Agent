"""管理回收必须跨中断重试，不能把停用、历史引用或未知目录当作可删安装。"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest

from knowledgeops_agent.harness.mcp.installations import MCPInstallStorage
from knowledgeops_agent.harness.mcp.management import MCPManagement
from knowledgeops_agent.harness.mcp.registry import MCPRegistry
from knowledgeops_agent.persistence.models import ExtensionOperation, MCPInstallation, utcnow


def test_interrupted_removal_uses_external_ownership_journal(tmp_path, monkeypatch):
    import shutil

    storage = MCPInstallStorage(tmp_path)
    connection, installation, operation = (str(uuid4()) for _ in range(3))
    directory = storage.create(connection, installation, operation)
    (directory / 'payload').write_text('partially removed')
    original = shutil.rmtree

    def partial(path, *args, **kwargs):
        (path / 'owner.json').unlink()
        (path / '.use.lock').unlink()
        raise OSError('injected file failure')

    monkeypatch.setattr(shutil, 'rmtree', partial)
    with pytest.raises(OSError):
        storage.remove(installation)
    monkeypatch.setattr(shutil, 'rmtree', original)
    # 新实例模拟维护器重启，不靠旧进程内存恢复归属。
    assert MCPInstallStorage(tmp_path).remove(installation)
    assert not directory.exists()


def test_removed_pending_installation_cannot_be_recreated_by_late_worker(tmp_path):
    storage = MCPInstallStorage(tmp_path)
    connection, installation, operation = (str(uuid4()) for _ in range(3))
    assert storage.remove(installation, connection=connection, operation=operation)
    with pytest.raises(ValueError, match='已经使用'):
        storage.create(connection, installation, operation)
    assert not storage.release(installation).exists()


@pytest.fixture
def manager(business_sessions, tmp_path, monkeypatch):
    monkeypatch.setenv('MCP_DIRECTORY', str(tmp_path))
    return MCPManagement(business_sessions, root=tmp_path)


def prepared(manager, **kwargs):
    item = manager.import_config({'name': 'owned', 'command': 'uvx', 'args': ['test-server']}, **kwargs)[0]
    queued = manager.prepare(item['id'])
    installation = queued['result']['installation_id']
    directory = manager.storage.create(item['target'], installation, item['id'])
    (directory / 'server').write_text('#!/bin/sh\nexit 0\n')
    (directory / 'server').chmod(0o755)
    manager.storage.complete(installation, {'entrypoint': 'server', 'request': {}, 'lock_files': {}})
    for area in ('cache', 'data'):
        (manager.storage.service_directory(area, item['target']) / 'payload').write_text(area)
    with manager.sessions.begin() as session:
        row = session.get(ExtensionOperation, item['id'])
        row.status = 'prepared'
        row.result = {**row.result, 'config': {'transport': 'stdio', 'command': str(directory / 'server'),
                      'installation_id': installation, 'cwd': str(manager.storage.checked('data', item['target']))}}
        session.get(MCPInstallation, installation).status = 'prepared'
    return item, installation, directory


def test_discard_waits_for_actual_use_and_then_cleans_owned_files(manager):
    item, installation, directory = prepared(manager)
    unknown = manager.storage.root / 'cache' / str(uuid4())
    unknown.mkdir()
    (unknown / 'keep').write_text('unregistered')
    manager.discard(item['id'])
    with manager.storage.use(installation):
        manager.maintain(expire=False)
        assert directory.exists()
        assert (manager.storage.checked('data', item['target']) / 'payload').exists()
    for _ in range(3):
        from knowledgeops_agent.harness.mcp.lifecycle import MCPCleaner
        for job in MCPCleaner(manager).list():
            if job['status'] in {'waiting', 'failed'}:
                MCPCleaner(manager).retry(job['id'])
        manager.maintain(expire=False)
    assert not directory.exists()
    assert not manager.storage.checked('data', item['target']).exists()
    assert not manager.storage.checked('cache', item['target']).exists()
    assert (unknown / 'keep').read_text() == 'unregistered'
    with manager.sessions() as session:
        assert session.get(MCPInstallation, installation).status == 'removed'


def test_disabled_connection_keeps_installation_but_deleted_connection_releases_it(manager):
    from knowledgeops_agent.harness.mcp.registry import configuration
    from knowledgeops_agent.persistence.models import MCPConnection

    item, installation, directory = prepared(manager)
    with manager.sessions.begin() as session:
        operation = session.get(ExtensionOperation, item['id'])
        config = configuration(operation.result['config'])
        row = MCPConnection(id=item['target'], name='disabled', config=config)
        session.add(row)
        MCPRegistry._save(session, row)
        operation.status = 'active'
    manager.maintain()
    assert directory.exists()
    registry = MCPRegistry(manager.sessions)
    registry.update(item['target'], expected_revision=1, deleted=True)
    for _ in range(3):
        manager.maintain(expire=False)
    assert not directory.exists()
    with manager.sessions() as session:
        assert session.get(MCPConnection, item['target']).deleted
        assert session.get(MCPInstallation, installation).request['manager'] == 'uv'


def test_expired_candidates_release_only_owned_unshared_project_values(manager):
    first = manager.import_config({'name': 'secret', 'url': 'http://localhost/mcp', 'headers': {'KEY': 'owned-secret'}})[0]
    ref = first['parameters']['config']['header_refs']['KEY']
    second = manager.import_config({'name': 'shared', 'url': 'http://localhost/mcp', 'headers': {'KEY': '${' + ref + '}'}})[0]
    manager.secrets.update({'MANUAL': 'leave-manual-value'})
    with manager.sessions.begin() as session:
        session.get(ExtensionOperation, first['id']).expires_at = utcnow() - timedelta(seconds=1)
    manager.maintain()
    assert manager.get(first['id'])['status'] == 'expired'
    assert manager.secrets.project_values()[ref] == 'owned-secret'
    manager.discard(second['id'])
    for _ in range(3):
        manager.maintain()
    assert ref not in manager.secrets.project_values()
    assert manager.secrets.project_values()['MANUAL'] == 'leave-manual-value'
    with manager.sessions() as session:
        assert 'owned-secret' not in json.dumps([row.parameters for row in session.query(ExtensionOperation)])


def test_unknown_call_protects_installation_and_data_until_resolved(manager):
    from knowledgeops_agent.harness.mcp.lifecycle import MCPCleaner
    from knowledgeops_agent.harness.mcp.registry import configuration
    from knowledgeops_agent.persistence.models import (
        MCPCall,
        MCPConnection,
        MCPConnectionRevision,
        Run,
    )

    item, installation, directory = prepared(manager)
    run_id, call_id = str(uuid4()), str(uuid4())
    with manager.sessions.begin() as session:
        operation = session.get(ExtensionOperation, item['id'])
        row = MCPConnection(id=item['target'], name='in-flight', config=configuration(operation.result['config']))
        session.add(row)
        MCPRegistry._save(session, row)
        operation.status = 'active'
        session.add(Run(id=run_id, root_id=run_id, checkpoint_id=run_id, task='unknown', budgets={}, status='interrupted'))
        session.flush()
        session.add(MCPCall(id=call_id, run_id=run_id, connection_id=row.id, connection_revision=1,
            tool_call_id='original-call', tool_name='lookup', input_hash='a'*64, request={}, status='unknown', authority_epoch=0))
    MCPRegistry(manager.sessions).update(item['target'], expected_revision=1, deleted=True)
    result = manager.maintain(expire=False)
    assert result['cleanup']['status'] == 'waiting'
    assert directory.exists() and manager.storage.checked('data', item['target']).exists()
    with manager.sessions.begin() as session:
        receipt = session.get(MCPCall, call_id)
        assert receipt.status == 'unknown' and receipt.tool_call_id == 'original-call'
        receipt.status = 'succeeded'  # 代表既有核对流程已得到原调用结果；清理器不执行工具。
    for job in MCPCleaner(manager).list():
        MCPCleaner(manager).retry(job['id'])
    manager.maintain(expire=False)
    assert not directory.exists() and not manager.storage.checked('data', item['target']).exists()
    with manager.sessions() as session:
        assert session.get(MCPConnectionRevision, (item['target'], 1)).content['config']['installation_id'] == installation
        assert session.get(MCPCall, call_id).status == 'succeeded'


def test_cleanup_failure_is_reported_and_retry_preserves_unowned_and_external_paths(manager, tmp_path, monkeypatch):
    from knowledgeops_agent.harness.mcp.lifecycle import MCPCleaner

    item, installation, directory = prepared(manager)
    external = tmp_path / 'external-command'
    external.write_text('outside ownership')
    manager.discard(item['id'])
    real_remove = manager.storage.remove

    def fail(*args, **kwargs):
        raise OSError('private-path-or-server-output')

    monkeypatch.setattr(manager.storage, 'remove', fail)
    assert manager.maintain(expire=False)['cleanup']['status'] == 'failed'
    job = MCPCleaner(manager).list()[0]
    assert job['error_code'] == 'owned_files_unavailable'
    assert 'private-path-or-server-output' not in json.dumps(job)
    assert directory.exists()
    monkeypatch.setattr(manager.storage, 'remove', real_remove)
    MCPCleaner(manager).retry(job['id'])
    assert manager.maintain(expire=False)['cleanup']['status'] == 'completed'
    assert not directory.exists() and external.read_text() == 'outside ownership'


def test_old_install_metadata_and_operation_logs_expire_after_references_disappear(manager):
    from knowledgeops_agent.app.lifecycle.configuration_history import ConfigurationHistoryCleaner
    from knowledgeops_agent.harness.mcp.lifecycle import MCPCleaner
    from knowledgeops_agent.harness.mcp.registry import configuration
    from knowledgeops_agent.persistence.models import (
        MCPConnection,
        MCPConnectionRevision,
        Run,
        RunResourceSnapshot,
    )

    item, installation, directory = prepared(manager)
    run_id = str(uuid4())
    with manager.sessions.begin() as session:
        operation = session.get(ExtensionOperation, item['id'])
        current = MCPConnection(id=item['target'], name='historical', config=configuration(operation.result['config']))
        session.add(current)
        MCPRegistry._save(session, current)
        old_binding = session.get(MCPConnectionRevision, (current.id, 1)).content
        operation.status = 'active'
        session.add(Run(id=run_id, root_id=run_id, checkpoint_id=run_id, task='history', budgets={}))
        session.flush()
        session.add(RunResourceSnapshot(root_id=run_id, content={'mcp': [old_binding]}, content_hash='a'*64))
    registry = MCPRegistry(manager.sessions)
    registry.update(item['target'], expected_revision=1, config={'transport': 'http', 'url': 'http://localhost/new'})
    manager.maintain()
    assert not directory.exists()  # 旧绑定已撤回；历史只需身份，不需要再启动旧程序。
    with manager.sessions.begin() as session:
        session.get(MCPInstallation, installation).removed_at = utcnow() - timedelta(days=31)
        session.get(ExtensionOperation, item['id']).updated_at = utcnow() - timedelta(days=31)
        for revision in session.query(MCPConnectionRevision):
            revision.created_at = utcnow() - timedelta(days=31)
    cleaner = ConfigurationHistoryCleaner(manager.sessions)
    assert cleaner.step('mcp')['removed'] == 0
    MCPCleaner(manager).prune()
    with manager.sessions.begin() as session:
        assert session.get(MCPInstallation, installation)
        assert session.get(ExtensionOperation, item['id']).result == {}
        session.delete(session.get(RunResourceSnapshot, run_id))
    assert cleaner.step('mcp')['removed'] == 1
    MCPCleaner(manager).prune()
    with manager.sessions() as session:
        assert session.get(MCPInstallation, installation) is None


def test_kept_installations_do_not_starve_later_cleanup_batches(manager):
    from knowledgeops_agent.harness.mcp.lifecycle import MCPCleaner
    from knowledgeops_agent.harness.mcp.registry import configuration
    from knowledgeops_agent.persistence.models import MCPConnection

    item, _, directory = prepared(manager)
    with manager.sessions.begin() as session:
        operation = session.get(ExtensionOperation, item['id'])
        row = MCPConnection(id=item['target'], name='kept', config=configuration(operation.result['config']))
        session.add(row)
        MCPRegistry._save(session, row)
        operation.status = 'active'
    candidate, _, disposable = prepared(manager, connection_id=item['target'], expected_revision=1)
    manager.discard(candidate['id'])
    assert MCPCleaner(manager).step(limit=1)['status'] == 'completed'
    assert directory.exists() and not disposable.exists()


def test_service_ownership_journals_expire_after_successful_cleanup(manager):
    from knowledgeops_agent.harness.mcp.lifecycle import MCPCleaner

    item, _, _ = prepared(manager)
    manager.discard(item['id'])
    assert MCPCleaner(manager).step()['status'] == 'completed'
    journal = manager.storage.checked('ownership', 'data', item['target'] + '.json')
    assert journal.exists()
    with manager.sessions.begin() as session:
        for row in session.query(ExtensionOperation).filter_by(kind='mcp_cleanup'):
            row.updated_at = utcnow() - timedelta(days=31)
    MCPCleaner(manager).prune()
    assert not journal.exists()
