"""真实保护进程的管道和退出：宿主断开/数据库会话丢失不能留下成功备份。"""

import json
import os
import selectors
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from knowledgeops_agent.persistence.maintenance import MaintenanceBusy, maintenance_lease


def receive(process):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(60), 'backup helper did not answer'
        return json.loads(process.stdout.readline())


@pytest.fixture
def isolated_backup():
    identity = uuid4().hex[:12]
    database = 'knowledgeops_backupfixture_' + identity
    admin = create_engine(os.environ['TEST_DATABASE_URL'], isolation_level='AUTOCOMMIT')
    assert admin.url.database == 'knowledgeops_test'
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE {database}'))
    engine = create_engine(admin.url.set(database=database))
    workspace = Path(f'/workspace/artifacts/verification/backup-{identity}/workspace')
    workspace.mkdir(parents=True)
    (workspace / 'source.txt').write_text('fixture data only')
    folder = Path('/workspace/artifacts/backups') / ('guard-' + identity)
    command = [sys.executable, 'scripts/backup_guard.py', '--database', database,
               '--operation-id', identity, '--workspace', str(workspace), '--folder', str(folder),
               '--fixture-root', str(workspace.parent / 'extensions')]
    try:
        yield engine, admin, workspace, folder, command
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE {database} WITH (FORCE)'))
        admin.dispose()


@pytest.mark.parametrize('failure', ['host_eof', 'lost_database_connection'])
def test_protection_process_failure_keeps_journal_and_releases_lease(isolated_backup, failure):
    engine, admin, workspace, folder, command = isolated_backup
    with (workspace.parent / 'guard-stderr.log').open('w') as log:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                   text=True, env={**os.environ, 'MODEL_CALLS_ENABLED': 'false'})
        try:
            ready = receive(process)
            assert ready['ok'] and ready['phase'] == 'ready'
            with pytest.raises(MaintenanceBusy):
                with maintenance_lease(engine, workspace=workspace):
                    pytest.fail('new writer admitted during backup')
            if failure == 'lost_database_connection':
                with admin.connect() as connection:
                    connection.execute(text('SELECT pg_terminate_backend(:pid)'), {'pid': ready['backend_pid']})
                process.stdin.write('{"command":"check"}\n')
                process.stdin.flush()
            else:
                process.stdin.close()
            response = receive(process)
            assert response['ok'] is False
            assert process.wait(timeout=15) == 1
            report = json.loads((folder / 'operation.json').read_text())
            assert report['state'] == 'failed'
            assert not (folder / 'manifest.json').exists()
            with maintenance_lease(engine, workspace=workspace):
                pass
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


def test_live_writer_is_reported_before_any_archive(isolated_backup):
    engine, _admin, workspace, folder, command = isolated_backup
    with maintenance_lease(engine, workspace=workspace):
        result = subprocess.run(command, text=True, capture_output=True, timeout=60,
                                env={**os.environ, 'MODEL_CALLS_ENABLED': 'false'})
        assert result.returncode == 1
        assert json.loads(result.stdout)['code'] == 'MaintenanceBusy'
    report = json.loads((folder / 'operation.json').read_text())
    assert report['state'] == 'failed' and report['phase'] == 'acquiring_protection'
    assert not list(folder.glob('*.dump')) and not (folder / 'manifest.json').exists()
