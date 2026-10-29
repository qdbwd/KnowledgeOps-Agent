"""旧终态初始化已读，等待操作仍需处理；迁移不改原任务与内容。"""

import os
from datetime import UTC, datetime
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, text
from sqlalchemy.orm import sessionmaker

from knowledgeops_agent.app.activity import ApplicationActivity


def test_activity_migration_preserves_old_state_and_does_not_create_historical_unread():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    assert engine.url.database == "knowledgeops_test"
    schema = "activity_migration_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0043")
            runs = Table("runs", MetaData(), autoload_with=connection)
            stamp = datetime(2026, 1, 1, tzinfo=UTC)
            for identity, status, outcome in [
                ("old-done", "completed", {"answer": "原始答案"}),
                ("old-approval", "interrupted", {"kind": "waiting_approval"}),
                ("old-unknown", "interrupted", {"kind": "execution_unknown"}),
                ("old-input", "waiting_input", {"question": "仍待回答"}),
            ]:
                connection.execute(runs.insert().values(id=identity, root_id=identity, checkpoint_id=identity,
                    task=identity, format_version=2, kind="agent", input_data={}, outcome=outcome,
                    cancel_requested=False, searches=0, event_sequence=0, created_at=stamp,
                    budgets={"model_requests": 10}, status=status, tool_calls=2, active_ms=100))
            before = {row["id"]: dict(row) for row in connection.execute(text("SELECT * FROM runs")).mappings()}
            command.upgrade(config, "0044")
            command.upgrade(config, "0044")
            for row in connection.execute(text("SELECT * FROM runs")).mappings():
                assert {key: row[key] for key in before[row["id"]]} == before[row["id"]]
            receipts = dict(connection.execute(text("SELECT run_id, version FROM run_read_receipts")).all())
            assert receipts == {"old-done": 1, "old-approval": 1, "old-unknown": 1}
        scoped = create_engine(engine.url, connect_args={"options": f"-csearch_path={schema}"})
        try:
            activity = ApplicationActivity(sessionmaker(scoped))
            summary = activity.summary()
            assert summary["counts"] == {"all": 4, "attention": 3, "active": 0, "unread": 0}
            assert {row["id"] for row in activity.page()["items"]} == {"old-approval", "old-unknown", "old-input"}
        finally:
            scoped.dispose()
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
