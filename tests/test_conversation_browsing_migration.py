"""0042旧聊天升级：只回填可证明的活动时间，不改旧内容或制造提交身份。"""

import os
from datetime import UTC, datetime
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, text


def test_old_chat_upgrade_preserves_values_and_adds_only_public_text_indexes():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    assert engine.url.database == "knowledgeops_test"
    schema = "chat_migration_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0042")
            stamp = datetime(2026, 1, 1, tzinfo=UTC)
            connection.execute(text("INSERT INTO conversations (id,title,created_at) VALUES ('old-chat','旧标题',:stamp),('prepared','准备中',:stamp)"), {"stamp": stamp})
            runs = Table("runs", MetaData(), autoload_with=connection)
            connection.execute(runs.insert().values(id="old-run", root_id="old-run", checkpoint_id="old-run",
                conversation_id="old-chat", task="旧任务", format_version=2, kind="agent", input_data={}, outcome={},
                cancel_requested=False, searches=0, event_sequence=0, created_at=stamp, budgets={},
                status="interrupted", tool_calls=0, active_ms=0))
            connection.execute(text("""INSERT INTO chat_messages (id,conversation_id,run_id,role,content,created_at,protocol_payload)
                VALUES ('old-user','old-chat','old-run','user','用户原话',:stamp,'{"reasoning":"不索引协议"}')"""), {"stamp": stamp})
            before_chat = dict(connection.execute(text("SELECT * FROM conversations WHERE id='old-chat'")).mappings().one())
            before_message = dict(connection.execute(text("SELECT * FROM chat_messages")).mappings().one())
            before_run = dict(connection.execute(text("SELECT * FROM runs")).mappings().one())
            command.upgrade(config, "0043")
            command.upgrade(config, "0043")
            after_chat = dict(connection.execute(text("SELECT * FROM conversations WHERE id='old-chat'")).mappings().one())
            after_run = dict(connection.execute(text("SELECT * FROM runs")).mappings().one())
            assert {k: after_chat[k] for k in before_chat} == before_chat
            assert {k: after_run[k] for k in before_run} == before_run
            assert dict(connection.execute(text("SELECT * FROM chat_messages")).mappings().one()) == before_message
            assert after_chat["activity_at"] == stamp and after_chat["messages_revision"] == 1
            assert after_chat["creation_key"] is None and after_run["submission_fingerprint"] is None
            assert connection.execute(text("SELECT activity_at FROM conversations WHERE id='prepared'")).scalar_one() is None
            indexes = dict(connection.execute(text("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=:schema"), {"schema": schema}).all())
            assert "USING gin (title " in indexes["ix_conversations_title_trgm"]
            assert "USING gin (content " in indexes["ix_chat_messages_content_trgm"]
            assert not any("protocol_payload" in definition for definition in indexes.values())
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
