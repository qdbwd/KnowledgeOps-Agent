"""正式迁移在隔离 schema 验证；不靠 ORM create_all 掩盖迁移遗漏。"""

import json
import os
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from knowledgeops_agent.db import Base


def test_fresh_migration_creates_all_business_tables_and_circular_fk():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    assert engine.url.database == "knowledgeops_test"
    # 名称完全由程序生成；只删除本测试创建的专用 schema。
    schema = "migration_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            inspector = inspect(connection)
            assert set(inspector.get_table_names(schema=schema)) == set(Base.metadata.tables) | {
                "alembic_version"
            }
            for name, table in Base.metadata.tables.items():
                assert {item["name"] for item in inspector.get_columns(name, schema=schema)} == {
                    column.name for column in table.columns
                }
            foreign_keys = inspector.get_foreign_keys("knowledge_documents", schema=schema)
            assert any(item["name"] == "fk_current_knowledge_version" for item in foreign_keys)
            # 重复升级不新建、不清空，版本命令也不依赖启动 API/worker。
            command.upgrade(config, "head")
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_upgrade_keeps_old_draft_data_and_marks_ambiguous_metadata():
    """旧草稿内容/修订不能被升级静默改写；语义版本由新列明确区分。"""
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    assert engine.url.database == "knowledgeops_test"
    schema = "migration_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0006")
            connection.execute(
                text("""INSERT INTO changesets
                (id,root_id,operations,status,proactive,requires_impact_confirmation,impact_confirmed,created_at,revision)
                VALUES ('old-draft','old-root','[]'::jsonb,'draft',false,false,false,now(),3)""")
            )
            command.upgrade(config, "head")
            command.upgrade(config, "head")
            row = connection.execute(
                text("SELECT id,revision,operations,operation_format FROM changesets")
            ).one()
            assert tuple(row) == ("old-draft", 3, [], 1)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_0015_preserves_old_role_binding_and_adds_empty_policy_tables():
    """新列允许NULL，旧角色和不可变修订不被补写成新的许可或资源哈希。"""
    from knowledgeops_agent.db import SubagentDefinition
    from knowledgeops_agent.subagent_registry import definition_view

    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    assert engine.url.database == "knowledgeops_test"
    schema = "tool_policy_migration_" + uuid4().hex
    old = {
        "id": "old-reader",
        "name": "old-reader",
        "display_name": "旧角色",
        "description": "读取材料",
        "system_prompt": "按证据回答",
        "tools": ["read_source"],
        "skills": [],
        "enabled": True,
        "deleted": False,
        "revision": 1,
        "authorization_epoch": 0,
        "builtin": False,
    }
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0014")
            connection.execute(
                text("""INSERT INTO subagent_definitions
                (id,name,display_name,description,system_prompt,tools,skills,revision,
                 authorization_epoch,enabled,deleted,created_at)
                VALUES (:id,:name,:display_name,:description,:system_prompt,
                        CAST(:tools AS jsonb),CAST(:skills AS jsonb),1,0,true,false,now())"""),
                {**old, "tools": json.dumps(old["tools"]), "skills": "[]"},
            )
            connection.execute(
                text("""INSERT INTO subagent_definition_revisions
                (definition_id,revision,content,created_at)
                VALUES (:id,1,CAST(:content AS jsonb),now())"""),
                {"id": old["id"], "content": json.dumps(old)},
            )
            before = dict(
                connection.execute(text("SELECT * FROM subagent_definitions")).mappings().one()
            )
            command.upgrade(config, "0015")
            command.upgrade(config, "0015")
            after = dict(
                connection.execute(text("SELECT * FROM subagent_definitions")).mappings().one()
            )
            assert {key: after[key] for key in before} == before
            assert after["tool_groups"] is None and after["resolved_tools"] is None
            assert (
                connection.execute(
                    text("SELECT content FROM subagent_definition_revisions")
                ).scalar_one()
                == old
            )
            with Session(bind=connection) as session:
                assert definition_view(session.get(SubagentDefinition, old["id"])) == old
            for table in ("tool_policies", "tool_policy_revisions"):
                assert connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_0016_adds_continuity_without_expanding_frozen_zero_policy():
    from knowledgeops_agent.tool_policy import baseline_policy

    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    assert engine.url.database == "knowledgeops_test"
    schema = "continuity_migration_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0015")
            command.upgrade(config, "0016")
            previous = connection.execute(
                text("SELECT content FROM tool_policy_revisions WHERE revision=0")
            ).scalar_one()
            assert previous == baseline_policy()
            current = connection.execute(text("SELECT content FROM tool_policies")).scalar_one()
            assert current["revision"] == 1
            assert current["enabled"]["builtin:history_search"] is True
            assert "builtin:history_search" not in previous["enabled"]
            assert {
                k: v for k, v in current["enabled"].items() if k in previous["enabled"]
            } == previous["enabled"]
            command.upgrade(config, "0018")
            assert (
                connection.execute(text("SELECT count(*) FROM tool_policy_revisions")).scalar_one()
                == 2
            )
            command.upgrade(config, "head")
            assert connection.execute(
                text("SELECT content FROM tool_policy_revisions WHERE revision=0")
            ).scalar_one() == previous
            assert connection.execute(
                text("SELECT content FROM tool_policy_revisions WHERE revision=1")
            ).scalar_one() == current
            latest = connection.execute(text("SELECT content FROM tool_policies")).scalar_one()
            assert latest["enabled"]["builtin:memory_search"] is True
            assert latest["enabled"]["builtin:memory_read"] is True
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
