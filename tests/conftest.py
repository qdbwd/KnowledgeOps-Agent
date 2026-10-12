"""业务测试各自使用临时 schema，避免互相清表，更不能触碰真实模型账本。"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from knowledgeops_agent.db import Base


@pytest.fixture(autouse=True)
def isolated_workspace_environment(monkeypatch):
    """测试必须显式注入临时工作区，不能继承 Compose 的真实挂载目录。"""
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)


@pytest.fixture
def business_sessions():
    url = os.environ["TEST_DATABASE_URL"]
    admin = create_engine(url)
    assert admin.url.database == "knowledgeops_test"
    schema = "test_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(
            text("CREATE EXTENSION IF NOT EXISTS pg_search WITH SCHEMA public CASCADE")
        )
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        Base.metadata.create_all(engine)
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
