"""旧回执备份在新restore库升级：只补结构，不封闭或重置历史预算。"""

import os
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from knowledgeops_agent.harness.models.gateway import (
    prepare_accounting,
    upgrade_accounting_lifecycle,
)


def test_old_accounting_restore_adds_lifecycle_column_without_changing_budget():
    url = make_url(os.environ["TEST_DATABASE_URL"])
    assert url.database == "knowledgeops_test"
    database = "knowledgeops_restore_" + uuid4().hex[:12] + "_accounting"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    engine = create_engine(url.set(database=database))
    created = False
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database}"'))
            created = True
        prepare_accounting(engine)
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE model_root_budgets DROP COLUMN closed_at"))
            connection.execute(text("INSERT INTO model_quotas(scope,used,canary_used) VALUES ('restored',0,0)"))
            connection.execute(text("INSERT INTO model_root_budgets(scope,root_id,request_limit,used) VALUES ('restored','root',3,2)"))
        upgrade_accounting_lifecycle(engine, scope="restored")
        upgrade_accounting_lifecycle(engine, scope="restored")
        with engine.connect() as connection:
            row = connection.execute(text("SELECT request_limit,used,closed_at FROM model_root_budgets")).one()
            assert tuple(row) == (3, 2, None)
            assert connection.execute(text("SELECT count(*) FROM accounting_migrations")).scalar_one() == 1
    finally:
        engine.dispose()
        if created:
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        admin.dispose()
