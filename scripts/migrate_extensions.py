"""同批格式3备份恢复成功后，将正式0039增量升级0042并核对所有旧列。"""

import argparse
import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from knowledgeops_agent.core.config import Settings
from knowledgeops_agent.persistence.backup import verified_manifest, verified_restore
from scripts.migrate_reliability import hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    backup = args.backup.resolve()
    if not backup.is_relative_to(Path('artifacts/backups').resolve()) or args.output.exists():
        raise ValueError('需要项目内备份和新的证据文件')
    manifest = verified_manifest(backup)
    restored = verified_restore(backup, source_database='knowledgeops')
    if manifest['format'] != 3 or not restored['checks'].get('extensions_restored_without_execution'):
        raise ValueError('需要包含完整扩展文件的同批恢复证据')
    settings = Settings()
    if settings.model_calls_enabled:
        raise ValueError('迁移需MODEL_CALLS_ENABLED=false')
    engine = create_engine(settings.database_url)
    if engine.url.database != 'knowledgeops':
        raise ValueError('此入口只升级正式knowledgeops库')
    report = {'from': '0039', 'to': '0042', 'backup': str(args.backup), 'model_calls': 0,
              'cleanup_performed': False, 'backup_manifest_sha256': manifest['manifest_sha256']}
    try:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout='10s'"))
            connection.execute(text('SELECT pg_advisory_xact_lock(748616002)'))
            if connection.scalar(text('SELECT version_num FROM alembic_version')) != '0039':
                raise ValueError('正式基线必须是0039；不能盲目重跑迁移')
            inspector = inspect(connection)
            names = sorted(set(inspector.get_table_names()) - {'alembic_version'})
            quote = connection.dialect.identifier_preparer.quote
            connection.execute(text('LOCK TABLE ' + ','.join(map(quote, names)) + ' IN ACCESS EXCLUSIVE MODE'))
            if connection.scalar(text("SELECT count(*) FROM runs WHERE status IN ('running','queued','waiting_children')")):
                raise ValueError('仍有活动任务，先停止并核对，不自动取消或重放')
            columns = {name: [item['name'] for item in inspector.get_columns(name)] for name in names}
            before = {name: hashes(connection, name, fields) for name, fields in columns.items()}
            config = Config('alembic.ini')
            config.attributes['connection'] = connection
            command.upgrade(config, '0042')
            after = {name: hashes(connection, name, fields) for name, fields in columns.items()}
            if before != after:
                raise ValueError('增量迁移改变了原有数据，整笔事务回滚')
            new_tables = ('run_skill_uses', 'skill_packages', 'extension_operations', 'mcp_installations')
            if any(connection.scalar(text('SELECT count(*) FROM ' + quote(name))) for name in new_tables):
                raise ValueError('结构升级不能推测补写使用记录或创建安装任务')
            report['original_tables_unchanged'] = after
        report['committed'] = True
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'schema': '0042', 'verified_tables': len(before), 'report': str(args.output)}))
    finally:
        engine.dispose()


if __name__ == '__main__':
    from knowledgeops_agent.persistence.maintenance import managed_main

    managed_main(main)
