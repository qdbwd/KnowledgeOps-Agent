"""别名迁移只增加结构；旧正文完整保留，有实际别名时禁止丢失映射的降级。"""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, text


def test_material_alias_migration_preserves_old_rows_and_guards_downgrade():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    assert engine.url.database == "knowledgeops_test"
    schema = "material_alias_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0034")
            metadata, stamp = MetaData(), datetime(2026, 1, 1, tzinfo=UTC)
            runs = Table("runs", metadata, autoload_with=connection)
            artifacts = Table("run_artifacts", metadata, autoload_with=connection)
            connection.execute(runs.insert().values(
                id="run", root_id="run", checkpoint_id="run", kind="agent", task="原要求",
                status="completed", input_data={}, budgets={}, outcome={}, cancel_requested=False,
                tool_calls=0, searches=0, active_ms=0, event_sequence=0, created_at=stamp,
            ))
            connection.execute(artifacts.insert().values(id="artifact", run_id="run", kind="tool_result",
                content_hash="a" * 64, content="旧正文", created_at=stamp))
            before = dict(connection.execute(text("SELECT * FROM run_artifacts")).mappings().one())
            command.upgrade(config, "0035")
            command.upgrade(config, "0035")
            after = connection.execute(text("SELECT * FROM run_artifacts")).mappings().one()
            assert {key: after[key] for key in before} == before
            assert after["promoted_version_id"] is None
            command.downgrade(config, "0034")
            command.upgrade(config, "0035")
            connection.execute(text("""INSERT INTO sources(id,identity_key,title,kind,created_at)
                VALUES ('source','source','固定正文','text',now())"""))
            connection.execute(text("""INSERT INTO source_versions(id,source_id,number,content_hash,mime_type,status,parser,created_at)
                VALUES ('version','source',1,:hash,'text/plain','ready','run-artifact-v1',now())"""), {"hash": "a" * 64})
            connection.execute(text("UPDATE run_artifacts SET promoted_version_id='version'"))
            with pytest.raises(RuntimeError, match="downgrade refused"):
                command.downgrade(config, "0034")
            connection.execute(text("DELETE FROM source_versions WHERE id='version'"))
            assert connection.execute(text("SELECT promoted_version_id FROM run_artifacts")).scalar_one() is None
            assert connection.execute(text("SELECT content FROM run_artifacts")).scalar_one() == "旧正文"
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
