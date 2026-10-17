"""用隔离 PG 和可控时间验证清理资格；终态、时间和依赖必须同时满足。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from knowledgeops_agent.app.lifecycle.service import LifecycleService
from knowledgeops_agent.persistence import models as db


class LedgerProbe:
    def __init__(self):
        self.pending = []

    def pending_root_calls(self, root_id):
        return list(self.pending)

    def settled_root_at(self, root_id):
        return None


@pytest.fixture
def lifecycle(business_sessions):
    clock = [datetime(2026, 10, 16, tzinfo=UTC)]
    ledger = LedgerProbe()
    service = LifecycleService(business_sessions, ledger, now=lambda: clock[0])
    return service, business_sessions, clock, ledger


def root(sessions, clock, *, status="completed", age=90):
    with sessions.begin() as session:
        identity = db.new_id()
        row = db.Run(
            id=identity, root_id=identity, checkpoint_id=identity, kind="material",
            task="生命周期夹具", budgets={}, status=status,
            created_at=clock[0] - timedelta(days=age + 1),
            finished_at=clock[0] - timedelta(days=age) if status == "completed" else None,
        )
        session.add(row)
    return identity


def test_old_terminal_gets_full_grace_preview_is_read_only_and_adoption_is_idempotent(lifecycle):
    service, sessions, clock, _ = lifecycle
    identity = root(sessions, clock)
    preview = service.preview_root(identity)
    assert not preview["eligible"]
    assert "retention_not_scheduled" in preview["blockers"]
    with sessions() as session:
        assert session.get(db.Run, identity).retention_adopted_at is None
    service.refresh_retention()
    first = service.preview_root(identity)
    assert first["details_expires_at"] == (clock[0] + timedelta(days=30)).isoformat()
    clock[0] += timedelta(days=29, hours=23)
    service.refresh_retention()
    assert service.preview_root(identity)["details_expires_at"] == first["details_expires_at"]
    assert not service.preview_root(identity)["eligible"]
    clock[0] += timedelta(hours=1)
    assert service.preview_root(identity)["eligible"]


@pytest.mark.parametrize("status", [
    "queued", "running", "interrupted", "paused", "waiting_input", "waiting_children",
])
def test_open_or_recoverable_roots_are_protected_even_with_stale_expiry(lifecycle, status):
    service, sessions, clock, _ = lifecycle
    identity = root(sessions, clock, status=status)
    with sessions.begin() as session:
        session.get(db.Run, identity).details_expires_at = clock[0] - timedelta(days=1)
    service.refresh_retention()
    preview = service.preview_root(identity)
    assert not preview["eligible"]
    assert f"run:{identity}:{status}" in preview["blockers"]
    assert preview["details_expires_at"] is None


def test_child_completion_is_not_result_consumption(lifecycle):
    service, sessions, clock, _ = lifecycle
    identity = root(sessions, clock)
    with sessions.begin() as session:
        child_id = db.new_id()
        session.add(db.Run(
            id=child_id, root_id=identity, parent_id=identity, checkpoint_id=child_id,
            task="子结果", budgets={}, status="completed", finished_at=clock[0],
        ))
        session.flush()
        notice = db.ChildNotification(
            root_id=identity, run_id=child_id, generation=1, event_key="completed", payload={},
        )
        session.add(notice)
        session.flush()
        notice_id = notice.id
    service.refresh_retention()
    assert f"child_notification:{notice_id}:unconsumed" in service.preview_root(identity)["blockers"]
    clock[0] += timedelta(days=40)
    with sessions.begin() as session:
        session.get(db.ChildNotification, notice_id).consumed_at = clock[0]
    service.refresh_retention()
    assert service.preview_root(identity)["details_expires_at"] == (clock[0] + timedelta(days=30)).isoformat()


def test_unknown_model_result_and_pending_handoff_prevent_expiry(lifecycle):
    service, sessions, clock, ledger = lifecycle
    identity = root(sessions, clock)
    ledger.pending = ["request-unknown"]
    service.refresh_retention()
    assert "model_request:request-unknown:unknown" in service.preview_root(identity)["blockers"]
    ledger.pending = []
    with sessions.begin() as session:
        action = db.ExecutionAction(
            root_id=identity, run_id=identity, tool_call_id="approval", tool_name="execute",
            arguments={}, fingerprint="a" * 64, workspace="/fixture", cwd="/", authority_epoch=0,
            batch_key="b" * 64, position=0, origin="agent", status="pending",
        )
        session.add(action)
        session.flush()
        action_id = action.id
    service.refresh_retention()
    assert f"execution_action:{action_id}:pending" in service.preview_root(identity)["blockers"]


def test_accounting_failure_is_not_interpreted_as_no_pending_calls(lifecycle, monkeypatch):
    service, sessions, clock, ledger = lifecycle
    identity = root(sessions, clock)
    def unavailable(_root_id):
        raise ConnectionError("fictional unavailable database")
    monkeypatch.setattr(ledger, "pending_root_calls", unavailable)
    assert "model_ledger_unavailable" in service.preview_root(identity)["blockers"]


def test_refresh_uses_bounded_keyset_batches(lifecycle):
    service, sessions, clock, _ = lifecycle
    identities = {root(sessions, clock) for _ in range(5)}
    first = service.refresh_retention(limit=2)
    assert first["processed"] == 2 and first["next_cursor"]
    second = service.refresh_retention(limit=2, after_id=first["next_cursor"])
    third = service.refresh_retention(limit=2, after_id=second["next_cursor"])
    assert third["processed"] == 1 and third["next_cursor"] is None
    with sessions() as session:
        adopted = set(session.scalars(select(db.Run.id).where(db.Run.retention_adopted_at.is_not(None))))
        assert adopted == identities


def test_fast_cancel_records_the_real_settlement_time_without_guessing_legacy_times(lifecycle):
    from knowledgeops_agent.app.runs.service import RunService

    service, sessions, clock, _ = lifecycle
    runs = RunService(sessions, now=lambda: clock[0])
    chat = runs.create_conversation()
    queued = runs.enqueue(chat["id"], "尚未执行", request_key="cancel")
    runs.cancel(queued["id"])
    service.refresh_retention()
    assert service.preview_root(queued["id"])["blockers"] == ["retention_period"]
    with sessions() as session:
        assert session.get(db.Run, queued["id"]).finished_at == clock[0]
    legacy = root(sessions, clock, status="cancelled")
    service.refresh_retention()
    assert f"run:{legacy}:missing_finished_at" in service.preview_root(legacy)["blockers"]
