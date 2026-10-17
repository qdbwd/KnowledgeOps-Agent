"""清理作业的持久交接：领取、崩溃、重试和并发均用真实隔离PG。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from knowledgeops_agent.app.lifecycle.jobs import CleanupJobs
from knowledgeops_agent.persistence import models as db


@pytest.fixture
def cleanup_jobs(business_sessions):
    clock = [datetime(2026, 10, 16, tzinfo=UTC)]
    jobs = CleanupJobs(business_sessions, now=lambda: clock[0], lease_seconds=30, retry_seconds=10)
    return jobs, business_sessions, clock


def test_intent_is_idempotent_and_only_one_worker_claims(cleanup_jobs):
    jobs, sessions, _ = cleanup_jobs
    with sessions.begin() as session:
        first = jobs.create(session, "run", "root")
        identity = first.id
    with sessions.begin() as session:
        assert jobs.create(session, "run", "root").id == identity
    with ThreadPoolExecutor(max_workers=3) as pool:
        claims = list(pool.map(lambda _: jobs.claim(), range(3)))
    assert len([claim for claim in claims if claim]) == 1
    claim = next(claim for claim in claims if claim)
    assert claim["id"] == identity and claim["phase"] == "prepare"
    assert jobs.get(identity)["status"] == "running"


def test_abandoned_claim_is_recovered_and_old_owner_cannot_commit(cleanup_jobs):
    jobs, sessions, clock = cleanup_jobs
    with sessions.begin() as session:
        identity = jobs.create(session, "run", "root").id
    abandoned = jobs.claim()
    assert jobs.claim() is None
    clock[0] += timedelta(seconds=31)
    resumed = jobs.claim()
    assert resumed["id"] == identity and resumed["claim_token"] != abandoned["claim_token"]
    with pytest.raises(ValueError, match="claim"):
        jobs.advance(identity, abandoned["claim_token"], phase="done")
    jobs.advance(identity, resumed["claim_token"], phase="checkpoints", cursor={"after": "child"})
    current = jobs.get(identity)
    assert current["phase"] == "checkpoints" and current["cursor"] == {"after": "child"}
    assert current["status"] == "pending" and current["claim_token"] is None


def test_failure_retains_phase_without_copying_sensitive_error_and_retry_is_explicit(cleanup_jobs):
    jobs, sessions, clock = cleanup_jobs
    with sessions.begin() as session:
        identity = jobs.create(session, "run", "root").id
    claim = jobs.claim()
    jobs.fail(identity, claim["claim_token"], RuntimeError("private request or secret"))
    failed = jobs.get(identity)
    assert failed["status"] == "failed" and failed["phase"] == "prepare"
    assert failed["last_error"] == "RuntimeError"
    assert jobs.claim() is None
    # 后台到期续跑和人工立即重试共用同一作业，不制造第二条删除意图。
    jobs.retry(identity)
    again = jobs.claim()
    assert again["id"] == identity
    jobs.advance(identity, again["claim_token"], phase="done", completed=True)
    assert jobs.get(identity)["status"] == "completed"
    assert jobs.retry(identity)["status"] == "completed"
    clock[0] += timedelta(days=29)
    assert jobs.prune_completed(retention_days=30, limit=1) == 0
    clock[0] += timedelta(days=1)
    assert jobs.prune_completed(retention_days=30, limit=1) == 1
    with sessions() as session:
        assert list(session.scalars(select(db.CleanupJob))) == []


def test_job_ttl_never_removes_pending_failed_or_live_claims(cleanup_jobs):
    jobs, sessions, clock = cleanup_jobs
    with sessions.begin() as session:
        for identity in ("pending", "failed", "running"):
            job = jobs.create(session, "run", identity)
            job.status = identity
            job.finished_at = clock[0] - timedelta(days=100)
    assert jobs.prune_completed(retention_days=30, limit=100) == 0


def test_completed_child_job_remains_until_waiting_parent_consumes_it(cleanup_jobs):
    jobs, sessions, clock = cleanup_jobs
    with sessions.begin() as session:
        child = jobs.create(session, "run", "root")
        child.status, child.finished_at = "completed", clock[0] - timedelta(days=31)
        parent = jobs.create(session, "conversation", "chat")
        parent.phase, parent.cursor = "conversation_runs", {"run_job": child.id, "root": "root"}
        parent.status = "failed"  # 即使父作业长期失败，进度凭据也不能被TTL回收。
        child_id, parent_id = child.id, parent.id
    assert jobs.prune_completed() == 0
    jobs.retry(parent_id)
    claim = jobs.claim(identity=parent_id)
    jobs.advance(parent_id, claim["claim_token"], phase="conversation_runs", cursor={"after": "root"})
    assert jobs.prune_completed() == 1
    with sessions() as session:
        assert session.get(db.CleanupJob, child_id) is None
        assert session.get(db.CleanupJob, parent_id) is not None
