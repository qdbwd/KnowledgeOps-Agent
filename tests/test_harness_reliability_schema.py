"""在真正的迁移路径验证旧数据保留与新增控制事实，禁止使用正常业务库。"""

import os
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, text

from knowledgeops_agent.persistence.models import utcnow

NEW_TABLES = {
    "run_controls", "file_operations", "run_questions", "mcp_continuations",
    "child_instructions", "child_notifications", "model_steps",
}


def test_0024_upgrade_preserves_pending_work_without_inventing_new_receipts():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    assert engine.url.database == "knowledgeops_test"
    schema = "reliability_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0024")
            # 旧版数据必须按旧表准备，不能让现行ORM默认值携带未来才出现的列。
            old_runs = Table("runs", MetaData(), autoload_with=connection)
            connection.execute(old_runs.insert().values(
                id="old", root_id="old", checkpoint_id="old-checkpoint", task="继续原任务",
                format_version=2, kind="agent", input_data={}, outcome={},
                cancel_requested=False, searches=0, event_sequence=0, created_at=utcnow(),
                budgets={"model_requests": 40, "tool_calls": 80, "searches": 20,
                         "active_seconds": 1200},
                status="interrupted", tool_calls=9, active_ms=2345,
            ))
            before = dict(connection.execute(text("SELECT * FROM runs")).mappings().one())
            command.upgrade(config, "head")
            command.upgrade(config, "head")
            after = dict(connection.execute(text("SELECT * FROM runs")).mappings().one())
            assert {key: after[key] for key in before} == before
            assert after["lifecycle_state"] == "active" and after["details_expires_at"] is None
            assert NEW_TABLES <= set(inspect(connection).get_table_names(schema=schema))
            for name in NEW_TABLES:
                assert connection.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one() == 0
            # 暂停/补问/等待子任务不是成功或失败，数据库允许独立状态。
            for status in ("paused", "waiting_input", "waiting_children"):
                connection.execute(text("UPDATE runs SET status=:status"), {"status": status})
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
