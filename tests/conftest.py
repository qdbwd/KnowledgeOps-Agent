"""业务测试各自使用临时 schema，避免互相清表，更不能触碰真实模型账本。"""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from knowledgeops_agent.persistence.models import Base

# 评估材料按发布范围仅在本地保留；缺少整个目录时不收集其专属测试。
# 本地目录存在时照常执行，不能吞掉评估代码或依赖自身的导入错误。
collect_ignore = [] if (Path(__file__).resolve().parents[1] / "evaluation").is_dir() else [
    "test_configured_delegation_evaluation.py",
    "test_evaluation_delivery_citations.py",
    "test_evaluation_runner.py",
    "test_evidence_manifest.py",
    "test_optimization_runner.py",
]


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
