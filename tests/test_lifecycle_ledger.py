"""清理后仍阻止旧调用重做；独立账本的结算、封闭与分批删除均走真实PG。"""

import pytest
from sqlalchemy import inspect, select, text, update
from test_model_gateway import ledger as _ledger

from knowledgeops_agent.harness.models import gateway as module

ledger = _ledger


def test_sealing_unknown_request_is_rejected_without_changing_budget(ledger):
    reservation, _ = ledger.reserve_call("root", "run", "agent", [], call_id="call", input_hash="hash")
    before = ledger.summary("root")
    with pytest.raises(ValueError, match="unsettled"):
        ledger.seal_root("root")
    ledger.finish(reservation, response={"content": "ok"})
    ledger.seal_root("root")
    ledger.seal_root("root")
    assert ledger.summary("root") == before
    with pytest.raises(module.ModelCallFailed, match="expired"):
        ledger.reserve_call("root", "run", "agent", [], call_id="call", input_hash="hash")
    with pytest.raises(ValueError, match="expired"):
        ledger.set_root_limit("root", 50)


def test_purge_is_bounded_scope_isolated_and_cannot_recreate_budget(ledger):
    for index in range(3):
        reservation, _ = ledger.reserve_call("root", "run", "agent", [{"content": "private"}], call_id=f"c{index}", input_hash=str(index))
        ledger.finish(reservation, response={"content": "private-result"})
    other = module.ModelLedger(ledger.engine, scope=ledger.scope + "-other")
    other.reserve("root", "another", "agent", [])
    with pytest.raises(ValueError, match="sealed"):
        ledger.purge_root_details("root", limit=2)
    ledger.seal_root("root")
    assert ledger.purge_root_details("root", limit=2) == {"deleted": 2, "remaining": True}
    assert ledger.purge_root_details("root", limit=2) == {"deleted": 1, "remaining": False}
    assert ledger.purge_root_details("root", limit=2) == {"deleted": 0, "remaining": False}
    assert ledger.records(root_id="root") == []
    assert len(other.records(root_id="root")) == 1
    assert ledger.summary("root")["root_used"] == 3
    with ledger.engine.connect() as connection:
        assert not list(connection.scalars(select(module.call_links.c.call_id).where(module.call_links.c.scope == ledger.scope)))
    with pytest.raises(module.ModelCallFailed, match="expired"):
        module.ModelLedger(ledger.engine, scope=ledger.scope).reserve("root", "new-run", "agent", [])


def test_root_without_model_requests_can_also_be_sealed(ledger):
    ledger.seal_root("never-called")
    assert ledger.purge_root_details("never-called") == {"deleted": 0, "remaining": False}
    with pytest.raises(module.ModelCallFailed, match="expired"):
        ledger.reserve("never-called", "run", "agent", [])


def test_legacy_terminal_status_without_settlement_timestamp_is_protected(ledger):
    identity = ledger.reserve("legacy", "run", "agent", [])
    ledger.finish(identity, response={"content": "old"})
    with ledger.engine.begin() as connection:
        connection.execute(update(module.requests).where(module.requests.c.id == identity).values(finished_at=None))
    assert ledger.pending_root_calls("legacy") == [identity]
    with pytest.raises(ValueError, match="unsettled"):
        ledger.seal_root("legacy")


def test_accounting_migration_is_additive_and_does_not_seal_old_roots(ledger):
    from scripts.migrate_lifecycle import upgrade_ledger

    reservation = ledger.reserve("old-root", "old-run", "agent", [{"content": "keep"}])
    ledger.finish(reservation, response={"content": "keep"})
    before = ledger.records(root_id="old-root")
    with ledger.engine.begin() as connection:
        connection.execute(text("ALTER TABLE model_root_budgets DROP COLUMN closed_at"))
    module.upgrade_accounting_lifecycle(ledger.engine, scope=ledger.scope)
    module.upgrade_accounting_lifecycle(ledger.engine, scope=ledger.scope)
    checked = upgrade_ledger(ledger.engine)
    assert checked["closed_old_roots"] == 0 and checked["old_migrations_preserved"] >= 1
    with ledger.engine.connect() as connection:
        assert "closed_at" in {row["name"] for row in inspect(connection).get_columns("model_root_budgets")}
        assert connection.scalar(select(module.roots.c.closed_at).where(module.roots.c.scope == ledger.scope)) is None
    assert ledger.records(root_id="old-root") == before
    assert ledger.summary("old-root")["root_used"] == 1
