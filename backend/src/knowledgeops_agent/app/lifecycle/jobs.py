"""清理意图的持久交接。这里只保存身份/阶段，不保存待删除正文或异常原文。"""

from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from knowledgeops_agent.persistence import models as db


def job_view(row: db.CleanupJob) -> dict:
    return {name: getattr(row, name) for name in (
        "id", "target_kind", "target_id", "status", "phase", "cursor", "summary",
        "attempts", "claim_token", "claim_until", "next_attempt_at", "last_error",
        "created_at", "updated_at", "finished_at",
    )}


class CleanupJobs:
    """一次领取只执行一个有界步骤；重启从已提交阶段继续，过期租约可接管。"""

    def __init__(
        self, sessions, *, now: Callable[[], datetime] = db.utcnow,
        lease_seconds: int = 60, retry_seconds: int = 60,
    ):
        if lease_seconds < 1 or retry_seconds < 1:
            raise ValueError("positive cleanup lease and retry interval required")
        self.sessions, self.now = sessions, now
        self.lease_seconds, self.retry_seconds = lease_seconds, retry_seconds

    def create(self, session, target_kind: str, target_id: str) -> db.CleanupJob:
        """由调用方与封闭业务对象同事务保存；唯一目标防止重复删除作业。"""
        if target_kind not in {"run", "conversation"}:
            raise ValueError("unsupported cleanup target")
        session.execute(insert(db.CleanupJob).values(
            id=db.new_id(), target_kind=target_kind, target_id=target_id,
            status="pending", phase="prepare", cursor={}, summary={}, attempts=0,
            next_attempt_at=self.now(), created_at=self.now(), updated_at=self.now(),
        ).on_conflict_do_nothing(index_elements=["target_kind", "target_id"]))
        return session.scalars(select(db.CleanupJob).where(
            db.CleanupJob.target_kind == target_kind, db.CleanupJob.target_id == target_id,
        )).one()

    def get(self, identity: str) -> dict:
        with self.sessions() as session:
            row = session.get(db.CleanupJob, identity)
            if row is None:
                raise KeyError("cleanup job not found")
            return job_view(row)

    def claim(self, *, identity: str | None = None, phases: tuple[str, ...] | None = None) -> dict | None:
        """多进程通过行锁竞争；并不依赖Redis通知只发送一次。"""
        stamp = self.now()
        query = select(db.CleanupJob).where(
            db.CleanupJob.status != "completed", db.CleanupJob.next_attempt_at <= stamp,
            or_(db.CleanupJob.claim_until.is_(None), db.CleanupJob.claim_until <= stamp),
        )
        if identity is not None:
            query = query.where(db.CleanupJob.id == identity)
        if phases is not None:
            query = query.where(db.CleanupJob.phase.in_(phases))
        with self.sessions.begin() as session:
            row = session.scalar(query.order_by(db.CleanupJob.next_attempt_at, db.CleanupJob.id)
                                 .with_for_update(skip_locked=True).limit(1))
            if row is None:
                return None
            row.claim_token, row.claim_until = db.new_id(), stamp + timedelta(seconds=self.lease_seconds)
            row.status, row.updated_at = "running", stamp
            row.attempts += 1
            return job_view(row)

    def owned(self, session, identity: str, token: str) -> db.CleanupJob:
        """步骤事务持有行锁，旧执行者不能提交新领取者的阶段/结果。"""
        row = session.scalar(select(db.CleanupJob).where(db.CleanupJob.id == identity).with_for_update())
        if row is None or row.status != "running" or row.claim_token != token or (
            row.claim_until is None or row.claim_until <= self.now()
        ):
            raise ValueError("cleanup claim is no longer owned")
        return row

    def advance_in(self, row: db.CleanupJob, *, phase: str, cursor: dict | None = None,
                   summary: dict | None = None, completed: bool = False) -> None:
        """与本步骤PG变更一并提交；外部操作须可重试，不能以阶段记录代替成功。"""
        row.phase, row.cursor = phase, cursor or {}
        if summary is not None:
            row.summary = summary
        row.status = "completed" if completed else "pending"
        row.claim_token, row.claim_until, row.last_error = None, None, None
        row.next_attempt_at = row.updated_at = self.now()
        row.finished_at = self.now() if completed else None

    def advance(self, identity: str, token: str, **changes) -> None:
        with self.sessions.begin() as session:
            self.advance_in(self.owned(session, identity, token), **changes)

    def fail(self, identity: str, token: str, error: Exception) -> None:
        with self.sessions.begin() as session:
            row = session.scalar(select(db.CleanupJob).where(db.CleanupJob.id == identity).with_for_update())
            if row is None or row.status != "running" or row.claim_token != token:
                return  # 已被接管时不能覆盖下一执行者的进度。
            row.status, row.last_error = "failed", type(error).__name__[:120]
            row.claim_token, row.claim_until = None, None
            row.updated_at = self.now()
            row.next_attempt_at = self.now() + timedelta(seconds=self.retry_seconds)

    def retry(self, identity: str) -> dict:
        with self.sessions.begin() as session:
            row = session.scalar(select(db.CleanupJob).where(db.CleanupJob.id == identity).with_for_update())
            if row is None:
                raise KeyError("cleanup job not found")
            if row.status == "failed":
                row.status, row.next_attempt_at, row.updated_at = "pending", self.now(), self.now()
            return job_view(row)

    def prune_completed(self, *, retention_days: int = 30, limit: int = 100) -> int:
        """完成满期且已被父作业消费才回收；失败父作业仍需要子作业的完成凭据。"""
        if retention_days < 1 or not 1 <= limit <= 1000:
            raise ValueError("invalid cleanup job retention or batch size")
        with self.sessions.begin() as session:
            parent = aliased(db.CleanupJob)
            awaited = select(parent.id).where(
                parent.status != "completed",
                parent.cursor["run_job"].astext == db.CleanupJob.id,
            ).correlate(db.CleanupJob).exists()
            identities = list(session.scalars(select(db.CleanupJob.id).where(
                db.CleanupJob.status == "completed",
                db.CleanupJob.finished_at <= self.now() - timedelta(days=retention_days),
                ~awaited,
            ).order_by(db.CleanupJob.finished_at, db.CleanupJob.id).limit(limit)
              .with_for_update(skip_locked=True)))
            session.execute(delete(db.CleanupJob).where(db.CleanupJob.id.in_(identities)))
            return len(identities)
