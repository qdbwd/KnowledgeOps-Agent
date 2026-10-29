"""真实PG会话与Linux文件锁：备份必须与所有合作写入者互斥。"""

import multiprocessing
import os
from pathlib import Path

import pytest
from sqlalchemy import text

from knowledgeops_agent.persistence.maintenance import (
    MaintenanceBusy,
    MaintenanceLost,
    maintenance_lease,
)


def test_writers_share_but_backup_is_exclusive(business_sessions, tmp_path):
    engine = business_sessions.kw['bind']
    with maintenance_lease(engine, workspace=tmp_path) as first:
        with maintenance_lease(engine, workspace=tmp_path) as second:
            first.check()
            second.check()
            with pytest.raises(MaintenanceBusy):
                with maintenance_lease(engine, workspace=tmp_path, exclusive=True):
                    pytest.fail('backup admitted while a writer is present')
    with maintenance_lease(engine, workspace=tmp_path, exclusive=True) as backup:
        backup.check()
        for exclusive in (False, True):
            with pytest.raises(MaintenanceBusy):
                with maintenance_lease(engine, workspace=tmp_path, exclusive=exclusive):
                    pytest.fail('concurrent writer/backup admitted')
    with maintenance_lease(engine, workspace=tmp_path):
        pass


def test_lost_database_session_cannot_be_replaced_and_file_guard_survives(business_sessions, tmp_path):
    engine = business_sessions.kw['bind']
    with pytest.raises(MaintenanceLost), maintenance_lease(engine, workspace=tmp_path) as writer:
        with engine.begin() as connection:
            connection.execute(text('SELECT pg_terminate_backend(:pid)'), {'pid': writer.backend_pid})
        for _ in range(2):
            with pytest.raises(MaintenanceLost):
                writer.check()
        # PG会话已消失，文件锁仍由活进程持有，不能开始跨数据库/文件备份。
        with pytest.raises(MaintenanceBusy):
            with maintenance_lease(engine, workspace=tmp_path, exclusive=True):
                pytest.fail('lost PG lease let backup race with live file writer')
    with maintenance_lease(engine, workspace=tmp_path, exclusive=True):
        pass


def test_lock_file_replacement_and_symlinks_fail_closed(business_sessions, tmp_path):
    engine = business_sessions.kw['bind']
    with pytest.raises(MaintenanceLost), maintenance_lease(engine, workspace=tmp_path) as lease:
        lease.lock_path.unlink()
        lease.lock_path.write_text('replaced')
        with pytest.raises(MaintenanceLost):
            lease.check()
    lease.lock_path.unlink()
    target = tmp_path / 'unrelated.txt'
    target.write_text('unchanged')
    lease.lock_path.symlink_to(target)
    with pytest.raises(MaintenanceBusy):
        with maintenance_lease(engine, workspace=tmp_path):
            pytest.fail('symlink lock file was accepted')
    assert target.read_text() == 'unchanged'


def _crash_holder(url, workspace, pipe):
    from sqlalchemy import create_engine

    with maintenance_lease(create_engine(url), workspace=Path(workspace)):
        pipe.send('held')
        pipe.recv()
        os._exit(17)


def test_process_crash_releases_both_locks(tmp_path):
    from sqlalchemy import create_engine

    # 独立子进程只连接隔离测试数据库，不需要业务表或正常运行数据。
    url = os.environ['TEST_DATABASE_URL']
    engine = create_engine(url)
    assert engine.url.database == 'knowledgeops_test'
    parent, child = multiprocessing.get_context('spawn').Pipe()
    process = multiprocessing.get_context('spawn').Process(target=_crash_holder, args=(url, str(tmp_path), child))
    process.start()
    try:
        assert parent.poll(15) and parent.recv() == 'held'
        with pytest.raises(MaintenanceBusy):
            with maintenance_lease(engine, workspace=tmp_path, exclusive=True):
                pytest.fail('live child lost exclusion')
        parent.send('exit')
        process.join(10)
        assert process.exitcode == 17
        with maintenance_lease(engine, workspace=tmp_path, exclusive=True) as lease:
            lease.check()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
        child.close()
        engine.dispose()


def test_service_construction_blocks_before_gateway_or_files(business_sessions, tmp_path, monkeypatch):
    from knowledgeops_agent.app import services
    from knowledgeops_agent.core.config import Settings

    engine = business_sessions.kw['bind']
    monkeypatch.setattr(services, 'create_engine', lambda *_a, **_k: engine)
    invoked = []
    monkeypatch.setattr(services, 'create_gateway', lambda *_: invoked.append('gateway'))
    settings = Settings(database_url=engine.url.render_as_string(hide_password=False),
                        workspace_root=tmp_path, source_storage_root=tmp_path / 'not-created',
                        model_calls_enabled=False)
    with maintenance_lease(engine, workspace=tmp_path, exclusive=True):
        with pytest.raises(MaintenanceBusy):
            services.create_services(settings)
        assert invoked == []
        assert not (tmp_path / 'not-created').exists()


def test_managed_command_rejected_before_entering_business_code(business_sessions, tmp_path, monkeypatch):
    from knowledgeops_agent.core import config
    from knowledgeops_agent.persistence.maintenance import managed_main

    engine = business_sessions.kw['bind']
    # 独立入口使用public，文件锁同时覆盖同工作区不同连接/schema，不能只靠表锁。
    settings = config.Settings(database_url=engine.url.render_as_string(hide_password=False),
                               workspace_root=tmp_path, model_calls_enabled=False)
    monkeypatch.setattr(config, 'get_settings', lambda: settings)
    invoked = []
    with maintenance_lease(engine, workspace=tmp_path, exclusive=True):
        with pytest.raises(MaintenanceBusy):
            managed_main(lambda: invoked.append('write'))
    assert invoked == []
    managed_main(lambda: invoked.append('write'))
    assert invoked == ['write']


def test_file_tools_cannot_remove_maintenance_control(tmp_path):
    from knowledgeops_agent.harness.sandbox.targets import mutable_path
    from knowledgeops_agent.persistence.maintenance import LOCK_FILE

    with pytest.raises(PermissionError):
        mutable_path(tmp_path, '/' + LOCK_FILE)


@pytest.mark.parametrize('command', [
    ['-m', 'alembic', 'upgrade', 'head'],
    ['scripts/migrate_accounting.py', '--lifecycle'],
    ['-m', 'knowledgeops_agent.worker'],
])
def test_actual_write_entrypoints_refuse_backup_window(business_sessions, tmp_path, command):
    import subprocess
    import sys

    engine = business_sessions.kw['bind']
    url = engine.url.render_as_string(hide_password=False)
    environment = {**os.environ, 'DATABASE_URL': url, 'ACCOUNTING_DATABASE_URL': url,
                   'WORKSPACE_ROOT': str(tmp_path), 'SOURCE_STORAGE_ROOT': str(tmp_path / 'sources'),
                   'MODEL_CALLS_ENABLED': 'false', 'RUNTIME_REDIS_URL': ''}
    with maintenance_lease(engine, workspace=tmp_path, exclusive=True):
        result = subprocess.run([sys.executable, *command], env=environment, capture_output=True,
                                text=True, timeout=90)
        assert result.returncode != 0
        assert 'MaintenanceBusy' in result.stderr
        assert not (tmp_path / 'sources').exists()
