"""真实旧表升级：只新增组目录，不能重写已保存许可或角色历史。"""

import json
import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from knowledgeops_agent.harness.tools.groups import default_groups


def test_group_migration_preserves_old_selection_and_versions():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    assert engine.url.database == "knowledgeops_test"
    schema = "group_migration_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0035")
            policy = {
                "id": "default",
                "revision": 77,
                "main_tools": [],
                "main_tool_groups": ["materials_read"],
                "resolved_main_tools": ["builtin:read_source"],
                "enabled": {"builtin:read_source": True},
                "main_epochs": {},
                "tool_epochs": {},
            }
            connection.execute(
                text("""INSERT INTO tool_policies VALUES (:id,77,CAST(:content AS jsonb))
                ON CONFLICT (id) DO UPDATE SET revision=EXCLUDED.revision, content=EXCLUDED.content"""),
                {"id": "default", "content": json.dumps(policy)},
            )
            connection.execute(
                text("""INSERT INTO tool_policy_revisions
                (policy_id,revision,content,created_at) VALUES ('default',77,CAST(:content AS jsonb),now())"""),
                {"content": json.dumps(policy)},
            )
            connection.execute(
                text("""INSERT INTO subagent_definitions
                (id,name,display_name,description,system_prompt,tools,skills,revision,
                 authorization_epoch,enabled,deleted,created_at,tool_groups,resolved_tools)
                VALUES ('role','reader','阅读者','只读','读取材料','[]','[]',3,2,true,false,now(),
                        '["materials_read"]','["read_source"]')""")
            )
            connection.execute(
                text("""INSERT INTO subagent_definition_revisions
                (definition_id,revision,content,created_at) VALUES ('role',3,CAST(:content AS jsonb),now())"""),
                {"content": json.dumps({"old": True})},
            )
            tables = [
                "tool_policies",
                "tool_policy_revisions",
                "subagent_definitions",
                "subagent_definition_revisions",
            ]
            before = {
                name: list(connection.execute(text(f"SELECT * FROM {name}")).mappings())
                for name in tables
            }
            command.upgrade(config, "0036")
            command.upgrade(config, "0036")
            for name, rows in before.items():
                assert list(connection.execute(text(f"SELECT * FROM {name}")).mappings()) == rows
            groups = list(
                connection.execute(text("SELECT content FROM tool_group_revisions")).scalars()
            )
            assert sorted(groups, key=lambda row: row["id"]) == sorted(
                default_groups(), key=lambda row: row["id"]
            )
            command.downgrade(config, "0035")
            command.upgrade(config, "0036")
            connection.execute(
                text("UPDATE tool_groups SET revision=2, deleted=true WHERE id='web'")
            )
            with pytest.raises(RuntimeError, match="downgrade refused"):
                command.downgrade(config, "0035")
            assert connection.execute(text("SELECT count(*) FROM tool_groups")).scalar_one() == 15
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
