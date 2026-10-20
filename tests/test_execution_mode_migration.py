"""旧自动执行降为逐次询问，历史请求和明确规则保留，不启用收费审批。"""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, text

from knowledgeops_agent.app.runs.service import RunService
from knowledgeops_agent.harness.permissions.settings import ExecutionSettings
from knowledgeops_agent.persistence.models import Conversation, ExecutionRule


def test_legacy_mode_is_read_as_ask_and_cannot_be_selected_again(business_sessions):
    jobs = RunService(business_sessions)
    conv = jobs.create_conversation("旧自动会话")["id"]
    with business_sessions.begin() as session:
        row = session.get(Conversation, conv)
        row.runtime_settings = {"permission_mode": "auto", "reasoning_effort": "high"}
    settings = ExecutionSettings(business_sessions)
    assert settings.get(conv)["permission_mode"] == "ask"
    assert settings.get(conv)["reasoning_effort"] == "high"
    with pytest.raises(ValueError):
        settings.update(conv, expected_revision=0, permission_mode="auto")
    assert settings.update(conv, expected_revision=0, permission_mode="auto_review")["permission_mode"] == "auto_review"


def test_upgrade_changes_only_current_legacy_settings_and_preserves_other_values():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    assert engine.url.database == "knowledgeops_test"
    schema = "execution_mode_migration_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0037")
            # 旧运行按0037实际表结构写入；当前ORM含后续迁移列，不能用于历史夹具。
            from sqlalchemy.orm import Session

            old_snapshot = {"runtime_settings": {"permission_mode": "auto", "revision": 3}}
            old_runs = Table("runs", MetaData(), autoload_with=connection)
            with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                for identity, mode in (("legacy", "auto"), ("human", "ask"), ("review", "auto_review")):
                    session.add(Conversation(id=identity, title=identity, runtime_revision=3,
                                             runtime_settings={"permission_mode": mode, "reasoning_effort": "high"}))
                session.flush()
                session.execute(old_runs.insert().values(
                    id="old-run", root_id="old-run", checkpoint_id="old-checkpoint",
                    conversation_id="legacy", kind="agent", task="旧任务", budgets={"model_requests": 9},
                    input_data=old_snapshot, status="interrupted", outcome={}, cancel_requested=False,
                    tool_calls=0, searches=0, active_ms=0, event_sequence=0,
                    created_at=datetime.now(UTC),
                ))
                session.add(ExecutionRule(id="kept-rule", workspace="/test", cwd="/test",
                                          conversation_id="legacy", argv=["git", "status"],
                                          match="exact", effect="allow"))
                session.commit()
            before = dict(connection.execute(text("SELECT id,runtime_settings FROM conversations")).all())
            command.upgrade(config, "0038")
            command.upgrade(config, "0038")
            rows = {row.id: row for row in connection.execute(
                text("SELECT id,runtime_settings,runtime_revision FROM conversations"))}
            assert rows["legacy"].runtime_settings == {**before["legacy"], "permission_mode": "ask"}
            assert rows["legacy"].runtime_revision == 4
            for identity in ("human", "review"):
                assert rows[identity].runtime_settings == before[identity]
                assert rows[identity].runtime_revision == 3
            assert connection.execute(text("SELECT input_data FROM runs WHERE id='old-run'")).scalar_one() == old_snapshot
            assert connection.execute(text("SELECT effect FROM execution_rules WHERE id='kept-rule'")).scalar_one() == "allow"
            with pytest.raises(RuntimeError, match="downgrade refused"):
                command.downgrade(config, "0037")
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
