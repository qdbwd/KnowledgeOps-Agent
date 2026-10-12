"""非空旧偏好升级，验证修订、原话和提取回执保留；只操作随机测试schema。"""

import json
import os
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from knowledgeops_agent.preferences import MemoryService


def test_nonempty_memory_upgrade_preserves_old_identity_versions_and_receipts():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    assert engine.url.database == "knowledgeops_test"
    schema = "memory_migration_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0018")
            connection.execute(
                text("""INSERT INTO preferences
                (id,revision,content,scope,topic,status,source,created_at)
                VALUES ('old-memory',1,'讲Python时先给例子','global','','active','{"kind":"manual"}',now())""")
            )
            old = {
                "id": "old-memory",
                "revision": 1,
                "content": "讲Python时先给例子",
                "scope": "global",
                "topic": "",
                "status": "active",
                "source_message_id": None,
                "source": {"kind": "manual"},
                "supersedes_id": None,
            }
            connection.execute(
                text("""INSERT INTO preference_revisions
                (preference_id,revision,content,created_at)
                VALUES ('old-memory',1,CAST(:content AS jsonb),now())"""),
                {"content": json.dumps(old)},
            )
            # 没有真人消息时不补造一个来源。旧快照内容也不因新分类而被重写。
            before = connection.execute(text("SELECT * FROM preferences")).mappings().one()
            policy_before = connection.execute(
                text("SELECT content FROM tool_policies WHERE id='default'")
            ).scalar_one()
            command.upgrade(config, "0019")
            command.upgrade(config, "0019")
            after = connection.execute(text("SELECT * FROM preferences")).mappings().one()
            assert {k: after[k] for k in before} == before
            assert after["category"] == "preference"
            assert (
                connection.execute(text("SELECT content FROM preference_revisions")).scalar_one()
                == old
            )
            evidence = connection.execute(text("SELECT * FROM memory_evidence")).mappings().one()
            assert evidence["message_id"] is None and evidence["quote"] == ""
            policy_after = connection.execute(
                text("SELECT content FROM tool_policies WHERE id='default'")
            ).scalar_one()
            assert policy_after["enabled"]["builtin:memory_search"]
            assert policy_after["enabled"]["builtin:memory_read"]
            assert all(policy_after["enabled"][k] == v for k, v in policy_before["enabled"].items())
            service = MemoryService(sessionmaker(connection, expire_on_commit=False))
            found = service.search("Python")
            assert found[0]["id"] == "old-memory" and found[0]["revision"] == 1
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
