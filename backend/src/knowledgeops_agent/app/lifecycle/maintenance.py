"""复用API维护循环的有界调度；PG记录扫描进度，Redis不拥有清理事实。"""

from datetime import timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert

from knowledgeops_agent.app.lifecycle.configuration_history import (
    KINDS,
    ConfigurationHistoryCleaner,
)
from knowledgeops_agent.app.lifecycle.details import RunDetailCleaner
from knowledgeops_agent.app.lifecycle.installed_content import collect_installations
from knowledgeops_agent.app.lifecycle.materials import RunRetentionCleaner
from knowledgeops_agent.app.lifecycle.retained import root_history_scopes
from knowledgeops_agent.app.lifecycle.workspace import refresh_workspace_references
from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.knowledge.files import workspace_lock
from knowledgeops_agent.persistence import models as db
from knowledgeops_agent.persistence.temporary import collect_temporaries

SCAN_STATE = "lifecycle-scan-v1"


class LifecycleMaintenance:
    def __init__(self, details, *, enabled=True, scan_interval_seconds=86400, job_retention_days=30, run_service=None,
                 temporary_roots=None, temporary_grace_seconds=86400, skill_manager=None, mcp_manager=None):
        from knowledgeops_agent.app.lifecycle.asset_cleanup import AssetCleaner
        from knowledgeops_agent.app.lifecycle.conversations import ConversationCleaner
        from knowledgeops_agent.app.lifecycle.knowledge import KnowledgeCleaner
        from knowledgeops_agent.app.lifecycle.legacy_html import LegacyHtmlCleaner
        from knowledgeops_agent.app.runs.service import RunService

        self.details, self.retained = details, RunRetentionCleaner(details)
        self.conversations = ConversationCleaner(details, run_service or RunService(details.sessions))
        self.sessions, self.now, self.jobs = details.sessions, details.now, details.jobs
        self.assets = AssetCleaner(self.sessions, self.jobs, batch_size=details.batch_size, eligibility=details.eligibility)
        self.knowledge = KnowledgeCleaner(self.sessions, self.jobs, batch_size=details.batch_size)
        self.legacy_html = LegacyHtmlCleaner(self.sessions, self.jobs)
        self.enabled, self.interval, self.job_retention = enabled, scan_interval_seconds, job_retention_days
        self.skill_manager = skill_manager
        self.mcp_manager = mcp_manager
        store = self.sessions.kw.get("info", {}).get("file_store")
        self.temporary_roots = list(dict.fromkeys(temporary_roots if temporary_roots is not None else [store.root] if store else []))
        self.temporary_grace = temporary_grace_seconds
        self.configuration = ConfigurationHistoryCleaner(
            self.sessions, now=self.now, retention_days=details.eligibility.retention_days)

    @classmethod
    def from_services(cls, services):
        from knowledgeops_agent.app.runs.worker import open_checkpointer
        from knowledgeops_agent.core.resources import managed_resource_root, resource_path
        from knowledgeops_agent.harness.context.usage import context_usage
        from knowledgeops_agent.harness.mcp.management import MCPManagement
        from knowledgeops_agent.harness.skills.management import SkillManagement

        settings = services.settings

        def usage(root_id):
            run = services.jobs.get_run(root_id)
            return context_usage(services.sessions, services.gateway, run["conversation_id"], only_run_id=root_id)

        details = RunDetailCleaner(
            services.sessions, services.gateway.ledger, lambda: open_checkpointer(services),
            batch_size=settings.lifecycle_batch_size, lease_seconds=settings.lifecycle_job_lease_seconds,
            retry_seconds=settings.lifecycle_retry_seconds, retention_days=settings.lifecycle_retention_days,
            event_bridge=services.event_bridge, checkpoint_cache=services.checkpoint_cache, usage_reader=usage,
        )
        return cls(details, enabled=settings.lifecycle_cleanup_enabled,
                   scan_interval_seconds=settings.lifecycle_scan_interval_seconds,
                   job_retention_days=settings.lifecycle_job_retention_days, run_service=services.jobs,
                   temporary_roots=[path for path in (settings.workspace_root, services.sources.storage_root) if path],
                   temporary_grace_seconds=settings.lifecycle_temporary_grace_seconds,
                   skill_manager=SkillManagement(services.sessions, resource_path("SKILLS_DIRECTORY", managed_resource_root("skills"))),
                   mcp_manager=MCPManagement(services.sessions))

    def tick(self) -> dict:
        """每次最多推进一个删除步骤和一个扫描批次；关闭自动扫描不遗弃已接受的作业。"""
        # 引用目录是删除预览的前提，关闭自动回收不应使人工删除永久等待回填。
        result = {"references": self.retained.catalog.step(limit=self.details.batch_size), "job": self.advance()}
        if self.enabled:
            result["scan"] = self.scan_step()
            result["jobs_expired"] = self.jobs.prune_completed(retention_days=self.job_retention, limit=self.details.batch_size)
        if self.skill_manager is not None:
            try:
                result["skills"] = self.skill_manager.maintain(expire=self.enabled, retention_days=self.job_retention)
            except Exception as error:
                # 独立重试：扩展目录故障不遗弃已经接受的会话/资产清理。
                result["skills"] = {"error": type(error).__name__}
        if self.mcp_manager is not None:
            try:
                result['mcp'] = self.mcp_manager.maintain(expire=self.enabled, retention_days=self.job_retention)
            except Exception as error:
                result['mcp'] = {'error': type(error).__name__}
        return result

    def advance(self, identity: str | None = None):
        with self.sessions() as session:
            query = select(db.CleanupJob).where(db.CleanupJob.target_kind.in_(["run", "conversation", "source", "delivery", "draft", "html_original", "knowledge"]), db.CleanupJob.status != "completed",
                db.CleanupJob.next_attempt_at <= self.now(),
                or_(db.CleanupJob.claim_until.is_(None), db.CleanupJob.claim_until <= self.now()))
            if identity:
                query = query.where(db.CleanupJob.id == identity)
            row = session.scalar(query.order_by(db.CleanupJob.next_attempt_at, db.CleanupJob.id).limit(1))
            if row is None:
                return None
            identity, phase = row.id, row.phase
        if phase in self.assets.PHASES:
            return self.assets.step(identity=identity)
        if phase in self.knowledge.PHASES:
            return self.knowledge.step(identity=identity)
        if phase in self.legacy_html.PHASES:
            return self.legacy_html.step(identity=identity)
        if phase in self.conversations.PHASES:
            return self.conversations.step(identity=identity)
        if phase in self.details.PHASES:
            return self.details.step(identity=identity)
        if phase in self.retained.PHASES:
            return self.retained.step(identity=identity)
        raise ValueError("cleanup job has an unsupported phase")

    def _claim_scan(self):
        with self.sessions.begin() as session:
            stamp = self.now()
            session.execute(insert(db.MaintenanceState).values(
                name=SCAN_STATE, cursor={"phase": "adopt"}, next_scan_at=stamp, updated_at=stamp,
            ).on_conflict_do_nothing())
            row = session.scalar(select(db.MaintenanceState).where(db.MaintenanceState.name == SCAN_STATE,
                db.MaintenanceState.next_scan_at <= stamp,
                or_(db.MaintenanceState.claim_until.is_(None), db.MaintenanceState.claim_until <= stamp),
            ).with_for_update(skip_locked=True))
            if row is None:
                return None
            row.claim_token, row.claim_until = db.new_id(), stamp + timedelta(seconds=self.jobs.lease_seconds)
            return row.claim_token, dict(row.cursor)

    def scan_step(self):
        claim = self._claim_scan()
        if claim is None:
            return None
        token, cursor = claim
        failure = None
        extra: dict = {}
        next_time = self.now()
        try:
            phase = cursor.get("phase", "adopt")
            if phase == "adopt":
                result = self.details.eligibility.refresh_retention(limit=self.details.batch_size, after_id=cursor.get("after"))
                cursor = {"phase": "adopt", "after": result["next_cursor"]} if result["next_cursor"] else {"phase": "expire"}
            elif phase == "expire":
                ids, more = self._roots("active", cursor.get("after"), due_only=True)
                for identity in ids:
                    preview = self.details.preview(identity)
                    if preview["eligible"]:
                        try:
                            self.details.admit(identity)
                        except ValueError:
                            # 预览之后有新依赖不算失败，下一次扫描按真实状态重新判断。
                            if self.details.preview(identity)["eligible"]:
                                raise
                cursor = {"phase": "expire", "after": ids[-1]} if more else {"phase": "workspace"}
            elif phase == "workspace":
                with workspace_lock(self.sessions), self.sessions.begin() as session:
                    lock_control(session)
                    refresh_workspace_references(session, session.info.get("file_store"))
                    closed = select(db.Run.id).where(db.Run.lifecycle_state != "active")
                    session.execute(delete(db.ArtifactRetentionReference).where(
                        db.ArtifactRetentionReference.owner_kind.in_(["run", "run_input", "checkpoint"]),
                        db.ArtifactRetentionReference.owner_id.in_(closed),
                    ))
                    session.execute(delete(db.AssetReference).where(
                        db.AssetReference.owner_kind.in_(["run", "run_input", "checkpoint"]), db.AssetReference.owner_id.in_(closed),
                    ))
                cursor = {"phase": "reconcile"}
            elif phase == "reconcile":
                ids, more = self._roots("expired", cursor.get("after"))
                for identity in ids:
                    self._reconcile(identity)
                cursor = {"phase": "reconcile", "after": ids[-1]} if more else {"phase": "assets"}
            elif phase == "assets":
                extra = self.assets.reconcile(after=cursor.get("after"), limit=self.details.batch_size)
                cursor = {"phase": "assets", "after": extra["next_cursor"]} if (
                    extra.get("waiting_for_references") or extra["next_cursor"]
                ) else {"phase": "knowledge"}
            elif phase == "knowledge":
                extra = self.knowledge.reconcile(after=cursor.get("after"), limit=self.details.batch_size)
                cursor = {"phase": "knowledge", "after": extra["next_cursor"]} if (
                    extra.get("waiting_for_references") or extra["next_cursor"]
                ) else {"phase": "content"}
            elif phase == "content":
                index = int(cursor.get("root", 0))
                if index < len(self.temporary_roots):
                    with self.sessions.begin() as session:
                        lock_control(session)
                        extra = collect_installations(session, self.temporary_roots[index], now=self.now(),
                            grace_seconds=self.temporary_grace, after=cursor.get("after"), limit=self.details.batch_size)
                    cursor = {"phase": "content", "root": index, "after": extra["next_cursor"]} if (
                        extra["busy"] or extra["next_cursor"]
                    ) else {"phase": "content", "root": index + 1}
                    if extra["busy"]:
                        next_time += timedelta(seconds=self.jobs.retry_seconds)
                if int(cursor.get("root", 0)) >= len(self.temporary_roots):
                    cursor = {"phase": "configuration", "kind": KINDS[0]}
            elif phase == "configuration":
                kind = cursor.get("kind", KINDS[0])
                extra = self.configuration.step(kind, after=cursor.get("after"), limit=self.details.batch_size)
                if extra["next_cursor"]:
                    cursor = {"phase": "configuration", "kind": kind, "after": extra["next_cursor"]}
                elif KINDS.index(kind) + 1 < len(KINDS):
                    cursor = {"phase": "configuration", "kind": KINDS[KINDS.index(kind) + 1]}
                else:
                    cursor = {"phase": "temporary"}
            elif phase == "temporary":
                index = int(cursor.get("root", 0))
                if index < len(self.temporary_roots):
                    with self.sessions.begin() as session:
                        lock_control(session)

                        def released(identity):
                            row = session.get(db.Run, identity)
                            return row is not None and row.lifecycle_state in {"expired", "deleted"}

                        extra = dict(collect_temporaries(self.temporary_roots[index], now=self.now(),
                            grace_seconds=self.temporary_grace, owner_released=released,
                            after=cursor.get("after"), limit=self.details.batch_size))
                    cursor = {"phase": "temporary", "root": index, "after": extra["next_cursor"]} if extra["next_cursor"] else {"phase": "temporary", "root": index + 1}
                if int(cursor.get("root", 0)) >= len(self.temporary_roots):
                    cursor = {"phase": "adopt", "temporary_summary": extra}
                    next_time += timedelta(seconds=self.interval)
            else:
                raise ValueError("unsupported lifecycle scan phase")
        except Exception as error:
            failure = type(error).__name__
            next_time = self.now() + timedelta(seconds=self.jobs.retry_seconds)
        with self.sessions.begin() as session:
            row = session.scalar(select(db.MaintenanceState).where(db.MaintenanceState.name == SCAN_STATE).with_for_update())
            if row.claim_token != token:
                return {"status": "superseded"}
            row.cursor = {**cursor, "last_error": failure} if failure else cursor
            row.claim_token, row.claim_until = None, None
            row.next_scan_at, row.updated_at = next_time, self.now()
        return {"phase": cursor.get("phase"), "error": failure, "next_scan_at": next_time.isoformat(), **extra}

    def _roots(self, lifecycle, after, *, due_only=False):
        query = select(db.Run.id).where(db.Run.id == db.Run.root_id, db.Run.lifecycle_state == lifecycle)
        if after:
            query = query.where(db.Run.id > after)
        if due_only:
            query = query.where(db.Run.details_expires_at <= self.now())
        with self.sessions() as session:
            ids = list(session.scalars(query.order_by(db.Run.id).limit(self.details.batch_size + 1)))
        return ids[:self.details.batch_size], len(ids) > self.details.batch_size

    def _reconcile(self, root_id):
        """引用随后消失或最新快照推进时，重新接续保留阶段，不重建执行或模型预算。"""
        with self.sessions.begin() as session:
            lock_control(session)
            root = session.get(db.Run, root_id)
            if root is None or root.lifecycle_state != "expired":
                return
            owned = select(db.Run.id).where(db.Run.root_id == root_id)
            # 已登记的检查点引用可能随后失效，必须重新读实际状态，不能把旧目录
            # 当作永久保留事实。只剩有效引用时本轮安全收敛，下一日再核对。
            material = session.scalar(select(db.RunArtifact.id).where(db.RunArtifact.run_id.in_(owned),
                db.RunArtifact.expired_at.is_(None)).limit(1))
            bound = select(db.Run.continuation_snapshot_id).where(db.Run.lifecycle_state == "active", db.Run.continuation_snapshot_id.is_not(None))
            latest = select(db.Conversation.latest_snapshot_id).where(db.Conversation.latest_snapshot_id.is_not(None))
            snapshot = session.scalar(select(db.ContinuationSnapshot.id).where(db.ContinuationSnapshot.run_id.in_(owned),
                db.ContinuationSnapshot.id.not_in(bound), db.ContinuationSnapshot.id.not_in(latest)).limit(1))
            history = session.scalar(select(db.TaskHistoryRecord.source_id).where(
                db.TaskHistoryRecord.scope.in_(root_history_scopes(session, root)),
            ).limit(1))
            if material is None and snapshot is None and history is None:
                return
            job = self.jobs.create(session, "run", root_id)
            if job.status not in {"pending", "completed"}:
                return
            root.lifecycle_state = "cleaning"
            self.jobs.advance_in(job, phase="retained_data")
