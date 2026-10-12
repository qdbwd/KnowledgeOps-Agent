"""用旧账本结构验证显式扩额：保留历史，不能靠进程重启或新 scope 扩额。"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from knowledgeops_agent import model_gateway as gateway


@pytest.fixture
def legacy_accounting():
    """只在测试库临时 schema 建立600上限，清理范围不接触正式账本。"""
    admin = create_engine(os.environ["TEST_DATABASE_URL"])
    assert admin.url.database == "knowledgeops_test"
    schema = "accounting_upgrade_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        os.environ["TEST_DATABASE_URL"], connect_args={"options": f"-csearch_path={schema}"}
    )
    with engine.begin() as connection:
        connection.execute(
            text("""
            CREATE TABLE model_quotas (
                scope varchar(120) PRIMARY KEY,
                total_limit integer NOT NULL CHECK(total_limit > 0 AND total_limit <= 600),
                used integer NOT NULL CHECK(used >= 0 AND used <= total_limit),
                canary_used integer NOT NULL CHECK(canary_used >= 0 AND canary_used <= 10)
            )
        """)
        )
    gateway.prepare_accounting(engine)
    ledger = gateway.ModelLedger(
        engine, legacy_project_accounting=True, scope="existing-scope", total_limit=600
    )
    first = ledger.reserve("old-root", "old-run", "canary", [], root_limit=3)
    ledger.finish(first, response={"content": "preserved"}, usage={"total_tokens": 12})
    failed = ledger.reserve("old-root", "old-run", "agent", [], root_limit=3)
    ledger.finish(failed, error="historical failure")
    ledger.reserve("old-root", "old-run", "agent", [], root_limit=3)
    try:
        yield engine, ledger
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_larger_process_setting_does_not_raise_persisted_quota(legacy_accounting):
    engine, _ = legacy_accounting
    reopened = gateway.ModelLedger(
        engine, legacy_project_accounting=True, scope="existing-scope", total_limit=1200
    )
    assert reopened.summary()["project_limit"] == 600
    assert reopened.summary()["project_used"] == 3


def test_explicit_migration_preserves_requests_and_canary_and_is_repeatable(legacy_accounting):
    engine, ledger = legacy_accounting
    before = ledger.records()
    result = gateway.upgrade_accounting_budget(engine, scope="existing-scope")
    assert result["previous_limit"] == 600
    assert result["total_limit"] == 1200
    assert result["used"] == 3
    assert ledger.records() == before
    reopened = gateway.ModelLedger(engine, legacy_project_accounting=True, scope="existing-scope")
    assert reopened.summary("old-root") == {
        "project_limit": 1200,
        "project_used": 3,
        "canary_used": 1,
        "root_limit": 3,
        "root_used": 3,
    }
    reopened.reserve("new-root", "new-run", "agent", [], root_limit=2)
    gateway.upgrade_accounting_budget(engine, scope="existing-scope")
    assert reopened.summary()["project_used"] == 4
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM model_quotas")) == 1
        assert connection.scalar(text("SELECT count(*) FROM accounting_migrations")) == 1


def test_upgrade_keeps_hard_limit_and_does_not_refund_unknown_requests(legacy_accounting):
    engine, ledger = legacy_accounting
    gateway.upgrade_accounting_budget(engine, scope="existing-scope")
    with engine.begin() as connection:
        # 仅构造接近边界的计数；真实旧记录，包括未知请求，必须保留。
        connection.execute(text("UPDATE model_quotas SET used=1199"))
    reopened = gateway.ModelLedger(engine, legacy_project_accounting=True, scope="existing-scope")
    reopened.reserve("last-root", "last-run", "agent", [], root_limit=2)
    with pytest.raises(gateway.ModelBudgetExceeded, match="total"):
        reopened.reserve("last-root", "last-run", "agent", [], root_limit=2)
    assert reopened.summary()["project_used"] == 1200
    assert ledger.records()[2]["status"] == "reserved"
    with pytest.raises(ValueError):
        gateway.ModelLedger(engine, legacy_project_accounting=True, total_limit=0)


def test_upgrade_rejects_unknown_scope_without_creating_new_allowance(legacy_accounting):
    engine, ledger = legacy_accounting
    with pytest.raises(ValueError, match="scope"):
        gateway.upgrade_accounting_budget(engine, scope="new-unapproved-scope")
    assert ledger.summary()["project_limit"] == 600
    assert len(ledger.records()) == 3


@pytest.mark.parametrize("target_limit", [1200, 1600, 2000])
def test_new_budget_rejects_unknown_scope(legacy_accounting, target_limit):
    engine, ledger = legacy_accounting
    with pytest.raises(ValueError, match="scope"):
        gateway.upgrade_accounting_budget(engine, scope="missing-scope", target_limit=target_limit)
    assert ledger.summary()["project_used"] == 3
    assert ledger.summary()["project_limit"] == 600


def test_1600_migration_preserves_1200_history_and_rejects_downgrade(legacy_accounting):
    engine, ledger = legacy_accounting
    gateway.upgrade_accounting_budget(engine, scope="existing-scope")
    before = ledger.records()
    reopened = gateway.ModelLedger(
        engine, legacy_project_accounting=True, scope="existing-scope", total_limit=1600
    )
    assert reopened.summary()["project_limit"] == 1200
    result = gateway.upgrade_accounting_budget(engine, scope="existing-scope", target_limit=1600)
    assert result == {
        "scope": "existing-scope",
        "previous_limit": 1200,
        "total_limit": 1600,
        "used": 3,
        "canary_used": 1,
    }
    assert ledger.records() == before
    assert reopened.summary("old-root")["root_used"] == 3
    reopened.reserve("new-root", "new-run", "agent", [], root_limit=2)
    repeated = gateway.upgrade_accounting_budget(engine, scope="existing-scope", target_limit=1600)
    assert repeated["previous_limit"] == 1600 and repeated["used"] == 4
    with engine.connect() as connection:
        receipts = connection.execute(
            text("SELECT version, details FROM accounting_migrations ORDER BY version")
        ).all()
    assert [row.version for row in receipts] == ["0001_budget_1200", "0003_budget_1600"]
    assert receipts[1].details["previous_limit"] == 1200
    with pytest.raises(ValueError, match="downgrade"):
        gateway.upgrade_accounting_budget(engine, scope="existing-scope", target_limit=1200)
    assert reopened.summary()["project_limit"] == 1600


def test_1600_limit_stays_hard_and_unapproved_target_is_rejected(legacy_accounting):
    engine, ledger = legacy_accounting
    with pytest.raises(ValueError, match="approved"):
        gateway.upgrade_accounting_budget(engine, scope="existing-scope", target_limit=1500)
    assert ledger.summary()["project_limit"] == 600
    gateway.upgrade_accounting_budget(engine, scope="existing-scope", target_limit=1600)
    with engine.begin() as connection:
        connection.execute(text("UPDATE model_quotas SET used=1599"))
    reopened = gateway.ModelLedger(
        engine, legacy_project_accounting=True, scope="existing-scope", total_limit=1600
    )
    reopened.reserve("last-root", "last-run", "agent", [], root_limit=2)
    with pytest.raises(gateway.ModelBudgetExceeded, match="total"):
        reopened.reserve("last-root", "last-run", "agent", [], root_limit=2)
    assert reopened.summary()["project_used"] == 1600
    assert ledger.records()[2]["status"] == "reserved"


def test_new_process_can_use_old_limit_without_implicit_migration(legacy_accounting):
    engine, _ = legacy_accounting
    ledger = gateway.ModelLedger(
        engine, legacy_project_accounting=True, scope="existing-scope", total_limit=1600
    )
    ledger.reserve("new-root", "new-run", "agent", [], root_limit=2)
    assert ledger.summary()["project_limit"] == 600
    assert ledger.summary()["project_used"] == 4


def test_2000_migration_preserves_1600_history_and_is_explicit(legacy_accounting):
    """普通启动不扩额；显式迁移保留请求、调用关联、根预算和旧迁移回执。"""
    engine, ledger = legacy_accounting
    gateway.upgrade_accounting_budget(engine, scope="existing-scope", target_limit=1600)
    reopened = gateway.ModelLedger(
        engine, legacy_project_accounting=True, scope="existing-scope", total_limit=2000
    )
    reopened.reserve_call(
        "new-root", "new-run", "agent", [], root_limit=2, call_id="once", input_hash="unchanged"
    )
    assert reopened.summary()["project_limit"] == 1600
    before_requests = ledger.records()
    tables = ("model_root_budgets", "model_call_links", "accounting_migrations")
    with engine.connect() as connection:
        before = {
            table: connection.execute(text(f"SELECT * FROM {table}")).mappings().all()
            for table in tables
        }

    result = gateway.upgrade_accounting_budget(engine, scope="existing-scope", target_limit=2000)
    assert result == {
        "scope": "existing-scope",
        "previous_limit": 1600,
        "total_limit": 2000,
        "used": 4,
        "canary_used": 1,
    }
    assert ledger.records() == before_requests
    assert reopened.summary("old-root")["root_used"] == 3
    repeated = gateway.upgrade_accounting_budget(engine, scope="existing-scope", target_limit=2000)
    assert repeated["previous_limit"] == 2000 and repeated["used"] == 4
    with engine.connect() as connection:
        for table in tables[:2]:
            assert (
                connection.execute(text(f"SELECT * FROM {table}")).mappings().all() == before[table]
            )
        receipts = (
            connection.execute(text("SELECT * FROM accounting_migrations ORDER BY version"))
            .mappings()
            .all()
        )
    assert receipts[:-1] == before["accounting_migrations"]
    assert receipts[-1]["version"] == "0004_budget_2000"
    assert receipts[-1]["details"]["previous_limit"] == 1600
    with pytest.raises(ValueError, match="downgrade"):
        gateway.upgrade_accounting_budget(engine, scope="existing-scope", target_limit=1600)
    assert reopened.summary()["project_limit"] == 2000


def test_2000_limit_still_blocks_the_next_request(legacy_accounting):
    """仅隔离夹具设为临界值；达到批准额度后不增加预占或退回未知请求。"""
    engine, ledger = legacy_accounting
    gateway.upgrade_accounting_budget(engine, scope="existing-scope", target_limit=2000)
    with engine.begin() as connection:
        connection.execute(text("UPDATE model_quotas SET used=1999"))
    reopened = gateway.ModelLedger(engine, legacy_project_accounting=True, scope="existing-scope")
    reopened.reserve("last-root", "last-run", "agent", [], root_limit=2)
    with pytest.raises(gateway.ModelBudgetExceeded, match="total"):
        reopened.reserve("last-root", "last-run", "agent", [], root_limit=2)
    assert reopened.summary()["project_used"] == 2000
    assert len(ledger.records()) == 4
    assert ledger.records()[2]["status"] == "reserved"


def test_remove_total_limit_keeps_history_and_root_budget(legacy_accounting):
    """累计统计可以超过旧上限，但原根预算、未知请求和旧回执不被清空。"""
    engine, ledger = legacy_accounting
    gateway.upgrade_accounting_budget(engine, scope="existing-scope", target_limit=2000)
    before = ledger.records()
    with engine.begin() as connection:
        # 仅隔离夹具模拟已用完旧总额度，避免制造2000条无价值的测试请求。
        connection.execute(text("UPDATE model_quotas SET used=2000"))
    result = gateway.remove_accounting_budget_limit(engine, scope="existing-scope")
    assert result == {
        "scope": "existing-scope",
        "previous_limit": 2000,
        "total_limit": None,
        "used": 2000,
        "canary_used": 1,
    }
    assert ledger.records() == before
    reopened = gateway.ModelLedger(engine, legacy_project_accounting=True, scope="existing-scope")
    assert reopened.summary()["project_limit"] is None
    with pytest.raises(gateway.ModelBudgetExceeded, match="root"):
        reopened.reserve("old-root", "old-run", "agent", [], root_limit=3)
    reopened.reserve("new-root", "new-run", "agent", [], root_limit=1)
    with pytest.raises(gateway.ModelBudgetExceeded, match="root"):
        reopened.reserve("new-root", "new-run", "agent", [], root_limit=1)
    repeated = gateway.remove_accounting_budget_limit(engine, scope="existing-scope")
    assert repeated["previous_limit"] is None and repeated["used"] == 2001
    assert reopened.summary("new-root")["root_used"] == 1
    assert ledger.records()[2]["status"] == "reserved"
    with engine.connect() as connection:
        receipts = connection.execute(
            text("SELECT version, details FROM accounting_migrations ORDER BY version")
        ).all()
    assert [row.version for row in receipts] == ["0004_budget_2000", "0005_remove_total_limit"]
    assert receipts[-1].details["previous_limit"] == 2000


def test_remove_total_limit_rejects_unknown_scope(legacy_accounting):
    engine, ledger = legacy_accounting
    with pytest.raises(ValueError, match="scope"):
        gateway.remove_accounting_budget_limit(engine, scope="new-unapproved-scope")
    assert ledger.summary()["project_limit"] == 600
    assert ledger.summary()["project_used"] == 3


def test_retirement_clears_only_aggregates_and_preserves_recovery(legacy_accounting):
    """历史调用仍能回放、旧根仍受预算约束，新调用不恢复项目计数。"""
    engine, legacy = legacy_accounting
    gateway.remove_accounting_budget_limit(engine, scope="existing-scope")
    legacy.reserve_call(
        "linked-root",
        "linked-run",
        "agent",
        [],
        root_limit=2,
        call_id="retained",
        input_hash="same",
    )
    tables = (gateway.requests, gateway.roots, gateway.call_links)
    with engine.connect() as connection:
        before = [connection.execute(table.select()).mappings().all() for table in tables]
    assert gateway.retire_project_usage(engine, scope="existing-scope")["project_usage_retired"]
    gateway.retire_project_usage(engine, scope="existing-scope")
    with engine.connect() as connection:
        for table, rows in zip(tables, before, strict=True):
            assert connection.execute(table.select()).mappings().all() == rows
        quota = connection.execute(gateway.quotas.select()).mappings().one()
        assert quota["used"] == quota["canary_used"] == 0
    current = gateway.ModelLedger(engine, scope="existing-scope")
    assert current.summary("old-root") == {"root_used": 3, "root_limit": 3}
    with pytest.raises(gateway.ModelBudgetExceeded, match="root"):
        current.reserve("old-root", "old-run", "agent", [], root_limit=3)
    _, is_new = current.reserve_call(
        "linked-root",
        "linked-run",
        "agent",
        [],
        root_limit=2,
        call_id="retained",
        input_hash="same",
    )
    assert not is_new
    current.reserve("new-root", "new-run", "agent", [], root_limit=1)
    assert current.summary("new-root") == {"root_used": 1, "root_limit": 1}
    with engine.connect() as connection:
        quota = connection.execute(gateway.quotas.select()).mappings().one()
        assert quota["used"] == quota["canary_used"] == 0


def test_current_runtime_ignores_stale_total_and_continues_old_request_order(legacy_accounting):
    """旧库总额度耗尽不能再拦截；新回执不能被旧全局序号遮住。"""
    engine, _ = legacy_accounting
    with engine.begin() as connection:
        connection.execute(text("UPDATE model_quotas SET used=600, canary_used=10"))
        connection.execute(text("UPDATE model_requests SET ordinal=100"))
    current = gateway.ModelLedger(engine, scope="existing-scope")
    current.set_root_limit("old-root", 4)
    reservation = current.reserve("old-root", "old-run", "agent", [{"new": True}], root_limit=4)
    current.finish(reservation, response={"content": "new"})
    assert current.record(reservation)["ordinal"] == 101
    assert current.latest_agent_request("old-run")["id"] == reservation
    with engine.connect() as connection:
        quota = connection.execute(gateway.quotas.select()).mappings().one()
        assert quota["used"] == 600 and quota["canary_used"] == 10
