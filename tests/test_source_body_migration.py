"""使用真实旧表与隔离PG演练正文迁移，旧ID、片段及哈希不可改写。"""

import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_source_body_upgrade_preserves_old_rows_and_refuses_lossy_downgrade():
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    assert engine.url.database == "knowledgeops_test"
    schema = "body_migration_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0030")
            connection.execute(
                text("""INSERT INTO sources
                (id, identity_key, title, kind, origin, created_at)
                VALUES ('old', 'web:https://example.org', '旧网页', 'web', 'https://example.org', now())""")
            )
            connection.execute(
                text("""INSERT INTO source_versions
                (id,source_id,number,content_hash,storage_key,mime_type,status,parser,created_at)
                VALUES ('old-v','old',1,:digest,:key,'text/html','ready','trafilatura-v2',now())"""),
                {"digest": "a" * 64, "key": "aa/" + "a" * 64},
            )
            connection.execute(
                text("""INSERT INTO source_segments
                (id,source_version_id,position,start_offset,end_offset,text,method,status)
                VALUES ('old-s','old-v',0,0,4,'旧正文。','text','ready')""")
            )
            connection.execute(
                text("""INSERT INTO evidence
                (id,root_id,source_segment_id,start_offset,end_offset,quote,created_at)
                VALUES ('old-e','old-root','old-s',0,4,'旧正文。',now())""")
            )
            tables = ("sources", "source_versions", "source_segments", "evidence")
            before = {
                name: dict(connection.execute(text(f'SELECT * FROM "{name}"')).mappings().one())
                for name in tables
            }
            command.upgrade(config, "0031")
            command.upgrade(config, "0031")
            for name, row in before.items():
                after = connection.execute(text(f'SELECT * FROM "{name}"')).mappings().one()
                assert {key: after[key] for key in row} == row
            new = connection.execute(text("SELECT * FROM source_versions")).mappings().one()
            assert new["content_hash_kind"] == "original"
            assert new["body_hash"] is None and new["original_hash"] is None
            assert next(
                c
                for c in inspect(connection).get_columns("source_versions", schema=schema)
                if c["name"] == "storage_key"
            )["nullable"]
            command.downgrade(config, "0030")
            command.upgrade(config, "0031")
            connection.execute(
                text("""INSERT INTO source_versions
                (id,source_id,number,content_hash,content_hash_kind,body_hash,body_format,
                 mime_type,status,parser,created_at)
                VALUES ('new-v','old',2,:digest,'body-v1',:body,'markdown','text/html','ready','md-v1',now())"""),
                {"digest": "b" * 64, "body": "c" * 64},
            )
            with pytest.raises(RuntimeError, match="downgrade refused"):
                command.downgrade(config, "0030")
            assert (
                connection.execute(text("SELECT count(*) FROM source_versions")).scalar_one() == 2
            )
            assert (
                connection.execute(
                    text("SELECT body_format FROM source_versions WHERE id='new-v'")
                ).scalar_one()
                == "markdown"
            )
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
