"""在已验证完整备份后升级0038到0039；校验所有旧列不变，不启动清理或模型。"""

import argparse
import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from knowledgeops_agent.core.config import Settings
from knowledgeops_agent.persistence.backup import verified_manifest
from scripts.migrate_reliability import hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    backup = args.backup.resolve()
    if not backup.is_relative_to(Path("artifacts/backups").resolve()) or args.output.exists():
        raise ValueError("require a project backup and a fresh report path")
    manifest = verified_manifest(backup)
    restored = json.loads((backup / "restore-verification.json").read_text(encoding="utf-8"))
    if manifest["source_database"] != "knowledgeops" or manifest["format"] != 2 or (
        restored.get("backup_manifest_sha256") != manifest["manifest_sha256"]
        or not restored.get("checks") or not all(restored["checks"].values())
    ):
        raise ValueError("require matching successful full-backup restore verification")
    settings = Settings()
    if settings.model_calls_enabled:
        raise ValueError("migration requires MODEL_CALLS_ENABLED=false")
    engine = create_engine(settings.database_url)
    if engine.url.database != "knowledgeops":
        raise ValueError("this entry point targets the normal database only")
    report = {"from": "0038", "to": "0039", "backup": str(args.backup),
              "backup_hashes_verified": True, "cleanup_performed": False, "model_calls": 0}
    try:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout='10s'"))
            connection.execute(text("SELECT pg_advisory_xact_lock(748616002)"))
            if connection.scalar(text("SELECT version_num FROM alembic_version")) != "0038":
                raise ValueError("expected schema 0038")
            inspector = inspect(connection)
            names = sorted(set(inspector.get_table_names()) - {"alembic_version"})
            quote = connection.dialect.identifier_preparer.quote
            connection.execute(text("LOCK TABLE " + ",".join(map(quote, names)) + " IN ACCESS EXCLUSIVE MODE"))
            if connection.scalar(text("SELECT count(*) FROM runs WHERE status IN ('queued','running','waiting_children')")):
                raise ValueError("active tasks must settle before migration")
            columns = {name: [col["name"] for col in inspector.get_columns(name)] for name in names}
            before = {name: hashes(connection, name, fields) for name, fields in columns.items()}
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0039")
            after = {name: hashes(connection, name, fields) for name, fields in columns.items()}
            if before != after:
                raise ValueError("existing data changed during additive migration")
            if connection.scalar(text("SELECT count(*) FROM knowledge_documents WHERE deleted_at IS NOT NULL")) or connection.scalar(
                text("SELECT count(*) FROM runs WHERE user_deleted_at IS NOT NULL")
            ):
                raise ValueError("migration must not mark existing data as deleted")
            report["original_tables_unchanged"] = after
        report["committed"] = True
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"schema": "0039", "verified_tables": len(before), "cleanup_performed": False,
                          "report": str(args.output)}))
    finally:
        engine.dispose()


if __name__ == "__main__":
    from knowledgeops_agent.persistence.maintenance import managed_main

    managed_main(main)
