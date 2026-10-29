"""备份不是几个成功命令的拼接：失败、文件变化与失锁都不能发布成功清单。"""

import json

import pytest

from knowledgeops_agent.persistence.backup import file_hash, verified_manifest
from knowledgeops_agent.persistence.backup_creation import BackupCreation


def begin(business_sessions, tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / '正文.md').write_text('固定正文\n')
    (workspace / 'empty').mkdir()
    (workspace / 'pointer.md').symlink_to('正文.md')
    return BackupCreation(business_sessions.kw['bind'], folder=tmp_path / 'backup',
                          workspace=workspace, skills=tmp_path / 'skills', mcp=tmp_path / 'mcp')


def database_archives(operation):
    # 这里仅验证发布协议与哈希绑定。真实pg_dump/pg_restore另由CLI整组演练证明。
    for name in ('business.dump', 'accounting.dump'):
        (operation.folder / name).write_bytes(b'validated dump stand-in')
    return {name: file_hash(operation.folder / name) for name in ('business.dump', 'accounting.dump')}


def test_publishes_only_after_files_and_database_checks(business_sessions, tmp_path):
    operation = begin(business_sessions, tmp_path)
    with operation:
        assert not (operation.folder / 'manifest.json').exists()
        operation.archive_files()
        with pytest.raises(ValueError):
            operation.publish({})
        operation.publish(database_archives(operation))
    manifest = verified_manifest(operation.folder)
    assert manifest['format'] == 3
    assert set(manifest['files']) == {'business.dump', 'accounting.dump', 'workspace.tar.gz', 'extensions.tar.gz'}
    assert json.loads((operation.folder / 'operation.json').read_text())['state'] == 'created'


@pytest.mark.parametrize('failure', ['body_change', 'new_file', 'removed_file', 'lost_lock', 'interrupted'])
def test_failed_operation_preserves_evidence_without_success_manifest(business_sessions, tmp_path, failure):
    from sqlalchemy import text

    operation = begin(business_sessions, tmp_path)
    with pytest.raises((RuntimeError, ValueError)):
        with operation:
            operation.archive_files()
            checked = database_archives(operation)
            if failure == 'body_change':
                (operation.workspace / '正文.md').write_text('被改写的正文')
            elif failure == 'new_file':
                (operation.workspace / 'new').write_text('new')
            elif failure == 'removed_file':
                (operation.workspace / '正文.md').unlink()
            elif failure == 'lost_lock':
                with business_sessions.kw['bind'].begin() as connection:
                    connection.execute(text('SELECT pg_terminate_backend(:pid)'), {'pid': operation.lease.backend_pid})
            else:
                raise RuntimeError('host interrupted')
            operation.publish(checked)
    assert not (operation.folder / 'manifest.json').exists()
    assert (operation.folder / 'workspace.tar.gz').exists()
    report = json.loads((operation.folder / 'operation.json').read_text())
    assert report['state'] == 'failed' and report['phase']


def test_existing_backup_is_never_overwritten(business_sessions, tmp_path):
    operation = begin(business_sessions, tmp_path)
    operation.folder.mkdir()
    old = operation.folder / 'manifest.json'
    old.write_text('existing backup')
    with pytest.raises(FileExistsError), operation:
        pass
    assert old.read_text() == 'existing backup'


def test_archive_bytes_are_rechecked_before_publish(business_sessions, tmp_path):
    operation = begin(business_sessions, tmp_path)
    with pytest.raises(ValueError), operation:
        operation.archive_files()
        checked = database_archives(operation)
        (operation.folder / 'business.dump').write_bytes(b'changed after pg_restore check')
        operation.publish(checked)
    assert not (operation.folder / 'manifest.json').exists()


def test_skill_changes_invalidate_same_set_backup(business_sessions, tmp_path):
    operation = begin(business_sessions, tmp_path)
    operation.skills.mkdir()
    (operation.skills / 'SKILL.md').write_text('# Skill\n正文')
    with pytest.raises(ValueError), operation:
        operation.archive_files()
        (operation.skills / 'SKILL.md').write_text('# Skill\n新的正文')
        operation.publish(database_archives(operation))
    assert not (operation.folder / 'manifest.json').exists()
