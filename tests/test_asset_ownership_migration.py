"""旧来源/草稿的未知意图保持保守，升级不推测归属、不改正文或文件。"""

import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def test_ownership_migration_preserves_legacy_and_refuses_lossy_downgrade():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    assert engine.url.database == "knowledgeops_test"
    schema = "ownership_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0032")
            connection.execute(text("""INSERT INTO sources(id,identity_key,title,kind,origin,created_at)
                VALUES ('old','old','临时附件','file','conversation:old',now())"""))
            connection.execute(text("""INSERT INTO changesets(id,root_id,operations,status,proactive,
                requires_impact_confirmation,impact_confirmed,created_at,workspace_paths)
                VALUES ('draft','old','[]','draft',false,false,false,now(),'{}')"""))
            before = {table: dict(connection.execute(text(f"SELECT * FROM {table}")).mappings().one())
                      for table in ("sources", "changesets")}
            command.upgrade(config, "0033")
            command.upgrade(config, "0033")
            for table, values in before.items():
                after = connection.execute(text(f"SELECT * FROM {table}")).mappings().one()
                assert {key: after[key] for key in values} == values
            assert connection.execute(text("SELECT ownership_known FROM sources")).scalar_one() is False
            assert connection.execute(text("SELECT retention_intent FROM changesets")).scalar_one() == "legacy"
            assert connection.execute(text("SELECT count(*) FROM source_uses")).scalar_one() == 0
            command.downgrade(config, "0032")
            command.upgrade(config, "0033")
            connection.execute(text("INSERT INTO source_uses VALUES ('old','library','default',now())"))
            with pytest.raises(RuntimeError, match="downgrade refused"):
                command.downgrade(config, "0032")
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
