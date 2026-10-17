"""已有完整备份后，将正常业务库升级至0029并迁移独立回执库；不执行清理。

在开发容器使用 ``python -m scripts.migrate_lifecycle``。两库不能一起提交，
每完成一库立即保存报告；失败时报告明确已提交部分，不能假称跨库回滚。
"""

import argparse
import hashlib
import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from knowledgeops_agent.core.config import Settings
from knowledgeops_agent.harness.models.gateway import upgrade_accounting_lifecycle
from scripts.migrate_reliability import hashes


def upgrade_business(connection):
    """在外部事务内迁移，并核对所有原表原列；校验失败时业务库DDL一起回滚。"""
    previous = connection.scalar(text("SELECT version_num FROM alembic_version"))
    if previous not in {"0028", "0029"}:
        raise ValueError("expected business schema 0028 or 0029")
    connection.execute(text("SET LOCAL lock_timeout='10s'"))
    inspector = inspect(connection)
    names = sorted(set(inspector.get_table_names()) - {"alembic_version"})
    quote = connection.dialect.identifier_preparer.quote
    connection.execute(text("LOCK TABLE " + ",".join(map(quote, names)) + " IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(text("SELECT count(*) FROM runs WHERE status IN ('running','queued')")):
        raise ValueError("stop active or queued application work before migrating")
    columns = {name: [column["name"] for column in inspector.get_columns(name)] for name in names}
    before = {name: hashes(connection, name, fields) for name, fields in columns.items()}
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "0029")
    after = {name: hashes(connection, name, fields) for name, fields in columns.items()}
    if before != after:
        raise ValueError("business migration changed an original table projection")
    added = sorted(set(inspect(connection).get_table_names()) - set(names) - {"alembic_version"})
    counts = {name: connection.scalar(text(f"SELECT count(*) FROM {quote(name)}")) for name in added}
    if any(counts.values()):
        raise ValueError("DDL migration unexpectedly created cleanup or reference records")
    if previous == "0028" and connection.scalar(text(
        "SELECT count(*) FROM runs WHERE retention_adopted_at IS NOT NULL OR details_expires_at IS NOT NULL "
        "OR lifecycle_state != 'active' OR details_summary != '{}'::jsonb"
    )):
        raise ValueError("DDL migration must not adopt, seal, or delete old runs")
    return {"from": previous, "to": "0029", "old_tables_unchanged": after, "new_tables": counts,
            "cleanup_performed": False, "adoption_remains_explicit": True}


def upgrade_ledger(engine):
    """复用现有迁移入口；迁移记录允许新增，其余旧列和预算必须原样保留。"""
    with engine.connect() as connection:
        inspector = inspect(connection)
        columns = {name: [c["name"] for c in inspector.get_columns(name)] for name in inspector.get_table_names()
                   if name != "accounting_migrations"}
        before = {name: hashes(connection, name, fields) for name, fields in columns.items()}
        old_migrations = list(connection.execute(text("SELECT * FROM accounting_migrations")).mappings())
    result = upgrade_accounting_lifecycle(engine)
    with engine.connect() as connection:
        after = {name: hashes(connection, name, fields) for name, fields in columns.items()}
        if before != after:
            raise ValueError("accounting migration changed existing requests or budgets")
        current_migrations = list(connection.execute(text("SELECT * FROM accounting_migrations")).mappings())
        if any(row not in current_migrations for row in old_migrations):
            raise ValueError("accounting migration replaced an existing migration record")
    return {**result, "old_tables_unchanged": after, "old_migrations_preserved": len(old_migrations)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    backup = args.backup.resolve()
    if not backup.is_relative_to(Path("artifacts/backups").resolve()) or args.output.exists():
        raise ValueError("require a project backup and a fresh report path")
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8-sig"))
    if manifest["source_database"] != "knowledgeops" or manifest["format"] != 2:
        raise ValueError("require a complete normal-database and workspace backup")
    for name in ("business.dump", "accounting.dump", "workspace.tar.gz"):
        with (backup / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != manifest["files"][name]:
                raise ValueError("backup checksum mismatch: " + name)
    settings = Settings()
    if settings.model_calls_enabled:
        raise ValueError("migration must run with MODEL_CALLS_ENABLED=false")
    business, accounting = create_engine(settings.database_url), create_engine(settings.accounting_database_url)
    if business.url.database != "knowledgeops" or accounting.url.database != "knowledgeops_accounting":
        raise ValueError("this entry point is only for the normal database pair")
    report = {"backup": str(args.backup), "backup_hashes_verified": True,
              "business_committed": False, "accounting_committed": False, "cleanup_performed": False}

    def save_report():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        with business.begin() as connection:
            report["business"] = upgrade_business(connection)
        report["business_committed"] = True
        save_report()
        report["accounting"] = upgrade_ledger(accounting)
        report["accounting_committed"] = True
        save_report()
        print(f"Migrated to 0029 / 0003_lifecycle; original columns verified; no cleanup performed; {args.output}")
    except Exception as error:
        # 若回执迁移后核对失败，迁移事实仍可在独立库复查；不泄露SQL参数或正文。
        with accounting.connect() as connection:
            report["accounting_committed"] = bool(connection.scalar(text(
                "SELECT count(*) FROM accounting_migrations WHERE version='0003_lifecycle'"
            )))
        report["error"] = type(error).__name__
        save_report()
        raise
    finally:
        business.dispose()
        accounting.dispose()


if __name__ == "__main__":
    main()
