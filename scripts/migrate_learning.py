"""在已停止api/worker的现有部署中升级学习模式，核对旧行与备份完整性。

只比较旧列，允许新增字段/行，不允许旧业务行被删除或改写。正常工具策略
升版单独检查：旧开关必须原样保留。报告只包含哈希和数量，不输出正文或密钥。
"""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from knowledgeops_agent.core.config import Settings


def row_hashes(connection, table, columns):
    quote = connection.dialect.identifier_preparer.quote
    rows = connection.execute(
        text(f"SELECT {','.join(quote(c) for c in columns)} FROM {quote(table)}")
    )
    return Counter(
        hashlib.sha256(
            json.dumps(dict(row), ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest()
        for row in rows.mappings()
    )


def snapshot(connection):
    inspector = inspect(connection)
    result = {}
    for table in inspector.get_table_names():
        if table == "alembic_version":
            continue
        columns = [
            c["name"]
            for c in inspector.get_columns(table)
            if not (table == "tool_policies" and c["name"] in {"revision", "content"})
        ]
        result[table] = (columns, row_hashes(connection, table, columns))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", required=True)
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/verification/learning-deployment.json")
    )
    args = parser.parse_args()
    manifest = json.loads((args.backup / "manifest.json").read_text(encoding="utf-8-sig"))
    for filename, expected in manifest["files"].items():
        if Path(filename).name != filename:
            raise ValueError("备份清单路径无效")
        actual = hashlib.file_digest((args.backup / filename).open("rb"), "sha256").hexdigest()
        if actual != expected:
            raise ValueError("备份文件哈希不一致")
    settings = Settings()
    business, accounting = (
        create_engine(settings.database_url),
        create_engine(settings.accounting_database_url),
    )
    try:
        with business.begin() as connection, accounting.connect() as ledger:
            active = connection.execute(
                text("SELECT count(*) FROM runs WHERE status IN ('queued','running')")
            ).scalar_one()
            if active:
                raise RuntimeError("仍有排队或运行任务，请先处理并停止服务")
            before_revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            if before_revision != "0018":
                raise RuntimeError("此交付迁移只接受0018基线；不要覆盖已存在的验证证据")
            old, old_ledger = snapshot(connection), snapshot(ledger)
            policy_before = connection.execute(
                text("SELECT content FROM tool_policies WHERE id='default'")
            ).scalar_one()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            checked = {}
            for table, (columns, hashes) in old.items():
                after = row_hashes(connection, table, columns)
                if hashes - after:
                    raise RuntimeError(f"旧行核对失败：{table}，迁移事务回滚")
                checked[table] = {
                    "old_rows_preserved": sum(hashes.values()),
                    "rows_after": sum(after.values()),
                }
            policy_after = connection.execute(
                text("SELECT content FROM tool_policies WHERE id='default'")
            ).scalar_one()
            if any(
                policy_after["enabled"].get(k) != v for k, v in policy_before["enabled"].items()
            ):
                raise RuntimeError("迁移改变了已有工具开关")
            for table, (columns, hashes) in old_ledger.items():
                if row_hashes(ledger, table, columns) != hashes:
                    raise RuntimeError("账本在迁移期间发生变化")
            result = {
                "before": "0018",
                "after": connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one(),
                "backup": str(args.backup),
                "backup_checksums_verified": True,
                "old_tables": checked,
                "accounting_unchanged": True,
                "tool_switches_preserved": True,
                "call_model_enabled": policy_after["enabled"].get("builtin:call_model", False),
                "new_learning_tasks": connection.execute(
                    text("SELECT count(*) FROM learning_tasks")
                ).scalar_one(),
                "model_calls_enabled": settings.model_calls_enabled,
            }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps({k: v for k, v in result.items() if k != "old_tables"}, ensure_ascii=False)
        )
    finally:
        business.dispose()
        accounting.dispose()


if __name__ == "__main__":
    from knowledgeops_agent.persistence.maintenance import managed_main

    managed_main(main)
