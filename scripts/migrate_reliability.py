"""0024到0028的有备份增量升级与原数据核对；只在项目开发容器显式运行。"""

import argparse
import hashlib
import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from knowledgeops_agent.core.config import Settings


def hashes(connection, name, columns, *, maximum_revision=None):
    quote = connection.dialect.identifier_preparer.quote
    predicate = " WHERE revision <= :revision" if maximum_revision is not None else ""
    sql = f"SELECT to_jsonb(t)::text FROM (SELECT {','.join(map(quote, columns))} FROM {quote(name)}{predicate}) t"
    values = sorted(hashlib.sha256(value.encode()).hexdigest() for value in connection.execute(
        text(sql), {"revision": maximum_revision} if maximum_revision is not None else {},
    ).scalars())
    return {"count": len(values), "sha256": hashlib.sha256("\n".join(values).encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    backup = args.backup.resolve()
    if not backup.is_relative_to(Path("artifacts/backups").resolve()) or args.output.exists():
        raise ValueError("require a project backup and a fresh report path")
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8-sig"))
    assert manifest["source_database"] == "knowledgeops" and manifest["format"] == 2
    for name in ("business.dump", "accounting.dump", "workspace.tar.gz"):
        with (backup / name).open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == manifest["files"][name]
    settings = Settings()
    assert not settings.model_calls_enabled
    engine = create_engine(settings.database_url)
    assert engine.url.database == "knowledgeops"
    report = {"from": "0024", "to": "0028", "backup": str(args.backup), "backup_hashes_verified": True}
    try:
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0024"
            # 取得排他事务锁，避免保存快照到比较期间其他客户端写入；超时即退出。
            connection.execute(text("SET LOCAL lock_timeout='10s'"))
            inspector = inspect(connection)
            names = sorted(set(inspector.get_table_names()) - {"alembic_version"})
            quote = connection.dialect.identifier_preparer.quote
            connection.execute(text("LOCK TABLE " + ",".join(map(quote, names)) + " IN ACCESS EXCLUSIVE MODE"))
            pending = connection.scalar(text("SELECT count(*) FROM runs WHERE status IN ('running','queued')"))
            if pending:
                raise ValueError("normal application has active/queued runs; stop and reconcile before migrating")
            before_policy = connection.execute(text("SELECT revision,content FROM tool_policies WHERE id='default'")).mappings().one()
            original_columns = {name: [col["name"] for col in inspector.get_columns(name)] for name in names}
            before = {name: hashes(connection, name, columns) for name, columns in original_columns.items() if name != "tool_policies"}
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0028")
            unchanged = {}
            for name, columns in original_columns.items():
                if name == "tool_policies":
                    continue
                after = hashes(connection, name, columns, maximum_revision=before_policy["revision"] if name == "tool_policy_revisions" else None)
                assert before[name] == after, f"existing data changed: {name}"
                unchanged[name] = after
            after_policy = connection.execute(text("SELECT revision,content FROM tool_policies WHERE id='default'")).mappings().one()
            assert after_policy["revision"] == before_policy["revision"] + 3
            original, changed = dict(before_policy["content"]), dict(after_policy["content"])
            assert all(changed["enabled"][key] == value for key, value in original["enabled"].items())
            assert {k: v for k, v in changed.items() if k not in {"enabled", "revision"}} == {
                k: v for k, v in original.items() if k not in {"enabled", "revision"}}
            added_tables = sorted(set(inspect(connection).get_table_names()) - set(names) - {"alembic_version"})
            counts = {name: connection.scalar(text(f"SELECT count(*) FROM {quote(name)}")) for name in added_tables}
            assert all(count == 0 for count in counts.values()), counts
            report.update(old_tables_unchanged=unchanged, new_tables=counts,
                          policy_revisions_added=3, old_permissions_and_selection_preserved=True)
        report["committed"] = True
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Migrated 0024 -> 0028; verified {len(unchanged)} original table projections; {args.output}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    from knowledgeops_agent.persistence.maintenance import managed_main

    managed_main(main)
