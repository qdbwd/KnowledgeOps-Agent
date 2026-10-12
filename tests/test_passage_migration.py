"""验证有旧数据的0016→0017，不以空库建表通过代替实际回填。"""

import json
import os
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker


def test_empty_database_enables_extension_and_builds_bm25_indexes():
    """扩展属于整个数据库；仅用已有扩展的临时schema不能验证新安装。"""
    admin = create_engine(os.environ["TEST_DATABASE_URL"], isolation_level="AUTOCOMMIT")
    assert admin.url.database == "knowledgeops_test"
    database = "knowledgeops_bm25_test_" + uuid4().hex
    fresh = create_engine(admin.url.set(database=database))
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database}" TEMPLATE template0'))
        with fresh.begin() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM pg_extension WHERE extname='pg_search'")
                ).scalar_one()
                == 0
            )
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            assert (
                connection.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname='pg_search'")
                ).scalar_one()
                == "0.26.1"
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_indexes WHERE indexname IN ('ix_history_chunks_bm25','ix_knowledge_chunks_bm25') AND indexdef LIKE '%USING paradedb%'"
                    )
                ).scalar_one()
                == 2
            )
    finally:
        fresh.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database}"'))
        admin.dispose()


def test_nonempty_backfill_preserves_sources_and_searches_late_chunks():
    from knowledgeops_agent.db import KnowledgeSearchChunk, TaskHistoryChunk
    from knowledgeops_agent.knowledge import KnowledgeService
    from knowledgeops_agent.retrieval_index import rebuild_history_index

    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    assert engine.url.database == "knowledgeops_test"
    schema = "passage_migration_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            # 从真实旧结构开始。不能先升head再降级：后续记忆/预算迁移有意拒绝
            # 有损降级；也不使用未来ORM/发布服务给旧结构造数据。
            command.upgrade(config, "0016")
            body = "背景段落。" * 500 + "\n\n旧正文末尾：蓝绿部署支持回滚。"
            connection.execute(
                text("INSERT INTO knowledge_documents(id, created_at) VALUES ('legacy-doc', now())")
            )
            connection.execute(
                text("""INSERT INTO knowledge_versions
                    (id, document_id, number, title, body, summary, category, tags,
                     archived, action, root_id, created_at)
                    VALUES ('legacy-version', 'legacy-doc', 1, '发布方法', :body, '', '',
                            '[]'::jsonb, false, 'create', 'migration-root', now())"""),
                {"body": body},
            )
            connection.execute(
                text("UPDATE knowledge_documents SET current_version_id='legacy-version'")
            )
            connection.execute(
                text("""INSERT INTO search_entries(document_id, version_id, vector, normalized_text)
                     VALUES ('legacy-doc', 'legacy-version', to_tsvector('simple', :body), :body)"""),
                {"body": body},
            )
            payload = {
                "id": "r" + "a" * 32,
                "role": "human",
                "message_id": "legacy",
                "name": None,
                "text": "说明。" * 1000 + "\n\n原话：先查回执。",
                "truncated": False,
            }
            connection.execute(
                text("""INSERT INTO task_history_records(scope, source_id, payload, search_terms)
                     VALUES (:scope, :source, CAST(:payload AS jsonb), ARRAY['回执'])"""),
                {"scope": "s" * 64, "source": payload["id"], "payload": json.dumps(payload)},
            )
            before = connection.execute(
                text("SELECT payload FROM task_history_records")
            ).scalar_one()
            versions = list(
                connection.execute(text("SELECT id, body FROM knowledge_versions ORDER BY id"))
            )
            command.upgrade(config, "head")
            command.upgrade(config, "head")
            sessions = sessionmaker(bind=connection, expire_on_commit=False)
            knowledge = KnowledgeService(sessions)
            hit = knowledge.search("蓝绿 回滚")[0]
            assert hit["start"] > 600 and "回滚" in hit["snippet"]
            assert (
                connection.execute(text("SELECT payload FROM task_history_records")).scalar_one()
                == before
            )
            assert (
                list(
                    connection.execute(text("SELECT id, body FROM knowledge_versions ORDER BY id"))
                )
                == versions
            )
            with sessions() as session:
                knowledge_chunks = list(session.scalars(select(KnowledgeSearchChunk)))
                history_chunks = list(session.scalars(select(TaskHistoryChunk)))
                assert len(knowledge_chunks) > 1 and len(history_chunks) > 1
            original_index = list(
                connection.execute(
                    text(
                        'SELECT scope, source_id, chunk_index, start, "end", body_tokens FROM task_history_chunks ORDER BY chunk_index'
                    )
                )
            )
            result = rebuild_history_index(sessions)
            assert result["indexed_records"] == 1
            assert (
                list(
                    connection.execute(
                        text(
                            'SELECT scope, source_id, chunk_index, start, "end", body_tokens FROM task_history_chunks ORDER BY chunk_index'
                        )
                    )
                )
                == original_index
            )
            assert json.loads(json.dumps(before)) == payload
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
