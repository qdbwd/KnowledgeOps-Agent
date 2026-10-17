"""从已交付0028升级，所有旧列保持原值；不在DDL里删除或伪造生命周期事实。"""

import os
from datetime import UTC, datetime
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, text

from scripts.migrate_lifecycle import upgrade_business


def test_0029_preserves_existing_payloads_and_leaves_backfill_explicit():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    assert engine.url.database == "knowledgeops_test"
    schema = "lifecycle_migration_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0028")
            metadata = MetaData()
            conversations = Table("conversations", metadata, autoload_with=connection)
            runs = Table("runs", metadata, autoload_with=connection)
            messages = Table("chat_messages", metadata, autoload_with=connection)
            artifacts = Table("run_artifacts", metadata, autoload_with=connection)
            old_time = datetime(2026, 1, 1, tzinfo=UTC)
            connection.execute(conversations.insert().values(id="chat", title="原会话", created_at=old_time))
            connection.execute(runs.insert().values(
                id="run", root_id="run", checkpoint_id="run", conversation_id="chat",
                kind="agent", task="原要求", status="completed", input_data={"original": True},
                budgets={"model_requests": 10}, outcome={"answer": "原答案"},
                cancel_requested=False, tool_calls=0, searches=0, active_ms=0, event_sequence=1,
                created_at=old_time, finished_at=old_time,
            ))
            connection.execute(messages.insert().values(
                id="message", conversation_id="chat", run_id="run", role="assistant",
                content="原答案", created_at=old_time,
            ))
            connection.execute(artifacts.insert().values(
                id="artifact", run_id="run", kind="context_original", content_hash="a" * 64,
                content="旧材料", created_at=old_time,
            ))
            before = {name: dict(connection.execute(text(f'SELECT * FROM "{name}"')).mappings().one())
                      for name in ("conversations", "runs", "chat_messages", "run_artifacts")}
            upgraded = upgrade_business(connection)
            assert upgraded["from"] == "0028" and not upgraded["cleanup_performed"]
            assert len(upgraded["old_tables_unchanged"]) > 50
            assert upgrade_business(connection)["from"] == "0029"
            for name, original in before.items():
                after = dict(connection.execute(text(f'SELECT * FROM "{name}"')).mappings().one())
                assert {key: after[key] for key in original} == original
            run = connection.execute(text("SELECT * FROM runs")).mappings().one()
            assert run["retention_adopted_at"] is None and run["details_expires_at"] is None
            assert run["details_summary"] == {} and run["lifecycle_state"] == "active"
            assert connection.execute(text("SELECT protocol_payload FROM chat_messages")).scalar_one() is None
            for table in ("cleanup_jobs", "artifact_retention_references", "maintenance_state"):
                assert connection.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one() == 0
            columns = {item["name"]: item for item in inspect(connection).get_columns("learning_tasks", schema=schema)}
            assert columns["conversation_id"]["nullable"] is True
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
