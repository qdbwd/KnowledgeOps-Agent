"""非空0036升级：动态展示标签不改正文、内部类别、旧修订与召回身份。"""

import json
import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def test_memory_category_upgrade_preserves_old_rows_and_snapshots():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    assert engine.url.database == "knowledgeops_test"
    schema = "memory_category_migration_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0036")
            connection.execute(
                text("""INSERT INTO preferences
                (id, revision, content, scope, topic, status, category, source, created_at)
                VALUES ('old',3,'我在学Python','global','','active','goal','{"kind":"manual"}',now())""")
            )
            old = {"id": "old", "revision": 3, "category": "goal", "content": "我在学Python"}
            connection.execute(
                text("""INSERT INTO preference_revisions
                (preference_id,revision,content,created_at) VALUES ('old',3,CAST(:value AS jsonb),now())"""),
                {"value": json.dumps(old)},
            )
            before = connection.execute(text("SELECT * FROM preferences")).mappings().one()
            command.upgrade(config, "0037")
            command.upgrade(config, "0037")
            after = connection.execute(text("SELECT * FROM preferences")).mappings().one()
            assert {key: after[key] for key in before} == before
            assert after["display_category_id"] == "memory-category-goal"
            assert after["classification"] == {"status": "classified", "source": "legacy"}
            assert (
                connection.execute(text("SELECT content FROM preference_revisions")).scalar_one()
                == old
            )
            assert (
                connection.execute(text("SELECT count(*) FROM memory_categories")).scalar_one() == 6
            )
            assert connection.execute(text("SELECT count(*) FROM runs")).scalar_one() == 0
            with pytest.raises(RuntimeError, match="downgrade refused"):
                command.downgrade(config, "0036")
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
