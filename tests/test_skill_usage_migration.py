"""新增技能使用事实不改旧数据，不从诊断事件推测用户实际使用了哪些包。"""

import os
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from knowledgeops_agent.persistence.models import MCPConnection, MCPConnectionRevision


def test_usage_migration_preserves_every_existing_table():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    assert engine.url.database == "knowledgeops_test"
    schema = "skill_usage_migration_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0039")
            connection.execute(MCPConnection.__table__.insert().values(
                id='existing-mcp', name='原连接', config={'transport': 'http', 'url': 'http://localhost/mcp'},
                tools=[{'name': 'read', 'permission': 'read'}], enabled=True))
            connection.execute(MCPConnectionRevision.__table__.insert().values(
                connection_id='existing-mcp', revision=1, content={'original': True}))
            before = {
                name: list(connection.execute(text(f'SELECT * FROM "{name}"')).mappings())
                for name in inspect(connection).get_table_names(schema=schema)
                if name != "alembic_version"
            }
            command.upgrade(config, "0042")
            command.upgrade(config, "0042")
            for name, rows in before.items():
                assert list(connection.execute(text(f'SELECT * FROM "{name}"')).mappings()) == rows
            assert connection.execute(text("SELECT count(*) FROM run_skill_uses")).scalar_one() == 0
            assert connection.execute(text("SELECT count(*) FROM skill_packages")).scalar_one() == 0
            assert connection.execute(text("SELECT count(*) FROM extension_operations")).scalar_one() == 0
            assert connection.execute(text("SELECT count(*) FROM mcp_installations")).scalar_one() == 0
            inspector = inspect(connection)
            assert inspector.get_pk_constraint("run_skill_uses", schema=schema)["constrained_columns"] == ["run_id", "resource"]
            foreign_key = inspector.get_foreign_keys("run_skill_uses", schema=schema)[0]
            assert foreign_key["referred_table"] == "runs"
            assert foreign_key["options"]["ondelete"] == "CASCADE"
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
