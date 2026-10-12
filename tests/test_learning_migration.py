"""非空0018升级保留旧聊天/运行/冻结资源，不凭迁移生成学习任务。"""

import os
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, select, text

from knowledgeops_agent.db import Conversation, Run, RunResourceSnapshot


def test_nonempty_upgrade_keeps_legacy_run_snapshot_and_disabled_tool():
    admin = create_engine(os.environ["TEST_DATABASE_URL"])
    assert admin.url.database == "knowledgeops_test"
    schema = "learning_upgrade_" + uuid4().hex
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0018")

            def insert_old(model, **values):
                old = Table(model.__tablename__, MetaData(), autoload_with=connection)
                for column in model.__table__.columns:
                    if (
                        column.name in old.c
                        and column.name not in values
                        and column.default is not None
                    ):
                        values[column.name] = (
                            column.default.arg(None)
                            if column.default.is_callable
                            else column.default.arg
                        )
                connection.execute(old.insert().values(**values))
                return dict(connection.execute(select(old)).mappings().one())

            insert_old(
                Conversation,
                id="old-conversation",
                title="历史会话",
                user_sequence=1,
                latest_root_sequence=1,
            )
            before_run = insert_old(
                Run,
                id="old-run",
                root_id="old-run",
                checkpoint_id="old-run",
                conversation_id="old-conversation",
                conversation_sequence=1,
                task="旧任务",
                status="completed",
                budgets={
                    "model_requests": 40,
                    "tool_calls": 80,
                    "searches": 20,
                    "active_seconds": 1200,
                },
            )
            before_snapshot = insert_old(
                RunResourceSnapshot,
                root_id="old-run",
                content={"format": "old-preserved", "preferences": []},
                content_hash="a" * 64,
            )
            connection.execute(
                text(
                    "UPDATE tool_policies SET content=jsonb_set(content, '{enabled,builtin:call_model}', 'false') WHERE id='default'"
                )
            )
            policy_before = connection.execute(
                text("SELECT content FROM tool_policies WHERE id='default'")
            ).scalar_one()
            command.upgrade(config, "head")
            command.upgrade(config, "head")
            run_after = dict(
                connection.execute(text("SELECT * FROM runs WHERE id='old-run'")).mappings().one()
            )
            assert {k: run_after[k] for k in before_run} == before_run
            assert run_after["execution_sequence"] == 1 and run_after["learning_task_id"] is None
            assert (
                dict(
                    connection.execute(text("SELECT * FROM run_resource_snapshots"))
                    .mappings()
                    .one()
                )
                == before_snapshot
            )
            assert connection.execute(text("SELECT count(*) FROM learning_tasks")).scalar_one() == 0
            policy_after = connection.execute(
                text("SELECT content FROM tool_policies WHERE id='default'")
            ).scalar_one()
            assert all(
                policy_after["enabled"][key] == value
                for key, value in policy_before["enabled"].items()
            )
            assert not policy_after["enabled"]["builtin:call_model"]
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM subagent_definitions WHERE id IN ('learning-reader','learning-reviewer')"
                    )
                ).scalar_one()
                == 2
            )
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
