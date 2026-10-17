"""详情阶段之后的保留计算、内部文件回收和结果收束。"""

from contextlib import nullcontext

from sqlalchemy import delete, or_, select

from knowledgeops_agent.app.lifecycle.catalog import ReferenceCatalog, install_reference_tracking
from knowledgeops_agent.app.lifecycle.files import (
    operation_tree_manifest,
    remove_artifact_file,
    remove_operation_file,
    remove_operation_tree_entry,
)
from knowledgeops_agent.app.lifecycle.references import reachable_artifacts, replace_references
from knowledgeops_agent.app.lifecycle.retained import (
    history_reachability,
    pin_checkpoint_artifacts,
    prune_history,
    prune_snapshots,
    root_history_scopes,
)
from knowledgeops_agent.app.lifecycle.workspace import refresh_workspace_references
from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.knowledge.files import workspace_lock
from knowledgeops_agent.persistence import models as db


class RunRetentionCleaner:
    """复用同一个CleanupJob，不复制任务正文；每次处理一批记录，失败保留原阶段。"""

    PHASES = ("retained_data", "retained_history", "retained_checkpoints", "retained_artifacts", "retained_files", "retained_file_tree", "retained_guidance", "finalize")

    def __init__(self, details):
        self.details = details
        self.sessions, self.jobs, self.now = details.sessions, details.jobs, details.now
        self.batch_size = details.batch_size
        self.catalog = ReferenceCatalog(self.sessions)
        install_reference_tracking(self.sessions)

    def step(self, *, identity: str | None = None) -> dict | None:
        claim = self.jobs.claim(identity=identity, phases=self.PHASES)
        if claim is None:
            return None
        try:
            # 回填拥有独立有界事务，避免嵌套控制锁死锁；未完成前材料阶段不前进。
            result = self.catalog.step(limit=self.batch_size)
            if not result["complete"]:
                self.jobs.advance(claim["id"], claim["claim_token"], phase=claim["phase"], cursor=claim["cursor"])
                return self.jobs.get(claim["id"])
            # 首次setup的并发索引不能放在一个尚未提交的业务事务内。
            with (self.details.saver() if claim["phase"] in {"retained_history", "retained_checkpoints"} else nullcontext()) as saver, workspace_lock(self.sessions), self.sessions.begin() as session:
                lock_control(session)
                job = self.jobs.owned(session, claim["id"], claim["claim_token"])
                root = self.details.eligibility._root(session, job.target_id)
                if root.lifecycle_state != "cleaning":
                    raise ValueError("retained cleanup requires a sealed run")
                if job.phase == "retained_data":
                    result = prune_snapshots(session, root.conversation_id, limit=self.batch_size) if root.conversation_id else {}
                    self.jobs.advance_in(job, phase="retained_data" if result.get("remaining") else "retained_history")
                elif job.phase == "retained_history":
                    self._history_step(session, root, job, saver)
                elif job.phase == "retained_checkpoints":
                    self._checkpoint_step(session, job, saver)
                elif job.phase == "retained_artifacts":
                    result = self.collect_materials(session, root.id, after=job.cursor.get("after"))
                    self.jobs.advance_in(job, phase="retained_artifacts" if result["more"] else "retained_files",
                                         cursor={"after": result["after"]} if result["more"] else {},
                                         summary={**job.summary, "materials_expired": job.summary.get("materials_expired", 0) + result["expired"]})
                elif job.phase == "retained_files":
                    self._file_step(session, root, job)
                elif job.phase == "retained_file_tree":
                    self._tree_step(session, root, job)
                elif job.phase == "retained_guidance":
                    self._guidance_step(session, root, job)
                elif job.phase == "finalize":
                    self._finalize(session, root, job)
        except Exception as error:
            self.jobs.fail(claim["id"], claim["claim_token"], error)
        return self.jobs.get(claim["id"])

    def _states(self, saver, run):
        """通过公开分页接口检查每个namespace的最新状态，包括尚未合并的写入。

        不把主图空namespace当成整个线程，也不直接查保存器内部表。每页有界，
        旧版本只用于发现尚未见到的子图，内存不累计完整检查点历史。
        """
        config = {"configurable": {"thread_id": run.checkpoint_id}}
        before, seen = None, set()
        while rows := list(saver.list(config, before=before, limit=self.batch_size)):
            for checkpoint in rows:
                namespace = checkpoint.config["configurable"].get("checkpoint_ns", "")
                if namespace in seen:
                    continue
                seen.add(namespace)
                yield checkpoint.checkpoint.get("channel_values", {})
                # 同一并行步可能有多条messages写入，不能dict覆盖后只保留最后一条。
                for _, channel, value in checkpoint.pending_writes or []:
                    yield {channel: value}
            before = rows[-1].config

    def _history_step(self, session, root, job, saver):
        scopes = sorted(scope for scope in root_history_scopes(session, root) if scope > job.cursor.get("after", ""))
        scope = job.cursor.get("scope") or next(iter(scopes), None)
        if scope is None:
            self.jobs.advance_in(job, phase="retained_checkpoints")
            return
        # 主历史按会话、子历史按Run；同会话的所有仍可恢复Run可能绑定不同快照。
        live = list(session.scalars(select(db.Run).where(db.Run.lifecycle_state == "active", or_(
            db.Run.conversation_id == root.conversation_id if root.conversation_id else db.Run.conversation_id.is_(None),
            db.Run.root_id == root.id,
        ))))
        states = [state for row in live for state in self._states(saver, row)]
        batches, sources = history_reachability(session, scope, states=states)
        result = prune_history(session, scope, protected_batches=batches, protected_sources=sources, limit=self.batch_size)
        self.jobs.advance_in(job, phase="retained_history", cursor={**job.cursor, "scope": scope} if result["remaining"] else {"after": scope})

    def _checkpoint_step(self, session, job, saver):
        query = select(db.Run).where(db.Run.lifecycle_state == "active")
        if job.cursor.get("after"):
            query = query.where(db.Run.id > job.cursor["after"])
        rows = list(session.scalars(query.order_by(db.Run.id).limit(self.batch_size + 1)))
        for row in rows[:self.batch_size]:
            pin_checkpoint_artifacts(session, row.id, {"states": list(self._states(saver, row))})
            replace_references(session, "run_input", row.id, {"task": row.task, "input": row.input_data, "outcome": row.outcome})
        more = len(rows) > self.batch_size
        self.jobs.advance_in(job, phase="retained_checkpoints" if more else "retained_artifacts",
                             cursor={"after": rows[self.batch_size - 1].id} if more else {})

    def collect_materials(self, session, root_id, *, after=None):
        """引用目录已回填、活检查点已核对后调用；后续维护也可再扫已过期根的材料。

        过期只是正文不可用，保留小型身份/哈希以说明失效。共享物理文件最后一个
        可用材料释放时才删除。外部删除成功但PG回滚时，重试相同地址仍可收敛。
        """
        if not self.catalog.ready(session):
            raise ValueError("reference catalog is incomplete")
        store = session.info.get("file_store")
        closed = select(db.Run.id).where(db.Run.lifecycle_state != "active")
        session.execute(delete(db.ArtifactRetentionReference).where(
            db.ArtifactRetentionReference.owner_kind.in_(["run", "run_input", "checkpoint"]),
            db.ArtifactRetentionReference.owner_id.in_(closed),
        ))
        refresh_workspace_references(session, store)
        owned = select(db.Run.id).where(db.Run.root_id == root_id, db.Run.lifecycle_state.in_(["cleaning", "expired"]))
        reachable = reachable_artifacts()
        query = select(db.RunArtifact).where(db.RunArtifact.run_id.in_(owned), db.RunArtifact.expired_at.is_(None),
                                             db.RunArtifact.id.not_in(select(reachable.c.id)))
        if after:
            query = query.where(db.RunArtifact.id > after)
        rows = list(session.scalars(query.order_by(db.RunArtifact.id).limit(self.batch_size + 1)))
        for row in rows[:self.batch_size]:
            if row.content_storage_key:
                shared = session.scalar(select(db.RunArtifact.id).where(
                    db.RunArtifact.content_storage_key == row.content_storage_key, db.RunArtifact.id != row.id,
                    db.RunArtifact.expired_at.is_(None),
                ).limit(1))
                if shared is None:
                    if store is None:
                        raise ValueError("material cleanup requires the configured workspace")
                    remove_artifact_file(store, run_id=row.run_id, content_hash=row.content_hash, storage_key=row.content_storage_key)
            row.expired_at, row._content, row.content_storage_key = self.now(), "", None
            session.execute(delete(db.ArtifactRetentionReference).where(
                db.ArtifactRetentionReference.owner_kind == "material", db.ArtifactRetentionReference.owner_id == row.id,
            ))
            session.flush()
        return {"expired": min(len(rows), self.batch_size), "more": len(rows) > self.batch_size,
                "after": rows[self.batch_size - 1].id if len(rows) > self.batch_size else None}

    def _file_step(self, session, root, job):
        rows = list(session.scalars(select(db.FileOperation).where(db.FileOperation.root_id == root.id)
                                   .order_by(db.FileOperation.id).limit(self.batch_size + 1)))
        store = session.info.get("file_store")
        for row in rows[:self.batch_size]:
            if row.status not in {"succeeded", "failed", "skipped"}:
                raise ValueError("file operation has an unresolved outcome")
            if store is None and (row.content_key or row.operation == "delete"):
                raise ValueError("file operation cleanup requires the configured workspace")
            if row.content_key:
                expected = f"artifacts/file-operations/{row.id}/content"
                if row.content_key != expected:
                    raise ValueError("file operation key differs from its owned address")
                remove_operation_file(store, operation_id=row.id, digest=row.after_hash)
            if row.operation == "delete" and row.before_hash:
                manifest = operation_tree_manifest(store, row.id, row.before_hash)
                if manifest is not None:
                    self.jobs.advance_in(job, phase="retained_file_tree", cursor={"operation": row.id, "entries": manifest})
                    return  # 先提交清单，下一个步骤才删除目录内容。
                remove_operation_file(store, operation_id=row.id, digest=row.before_hash, deleted=True)
            session.delete(row)
        self.jobs.advance_in(job, phase="retained_files" if len(rows) > self.batch_size else "retained_guidance")

    def _tree_step(self, session, root, job):
        row = session.get(db.FileOperation, job.cursor["operation"])
        if row is None or row.root_id != root.id or row.operation != "delete" or row.status not in {"succeeded", "failed", "skipped"}:
            raise ValueError("operation archive no longer belongs to this cleanup")
        store = session.info.get("file_store")
        entries = job.cursor["entries"]
        for entry in entries[:self.batch_size]:
            remove_operation_tree_entry(store, row.id, entry)
        remaining = entries[self.batch_size:]
        if not remaining:
            session.delete(row)
        self.jobs.advance_in(job, phase="retained_file_tree" if remaining else "retained_files",
                             cursor={"operation": row.id, "entries": remaining} if remaining else {})

    def _guidance_step(self, session, root, job):
        # 投递身份还决定聊天排序，随聊天保存；解释结果只是执行详情，可以释放。
        closed = select(db.Run.id).where(db.Run.lifecycle_state.in_(["cleaning", "expired"]))
        owned = select(db.Run.id).where(db.Run.root_id == root.id)
        rows = list(session.scalars(select(db.MessageDelivery).where(
            or_(db.MessageDelivery.target_root_id == root.id, db.MessageDelivery.source_run_id.in_(owned)),
            db.MessageDelivery.target_root_id.in_(closed), db.MessageDelivery.source_run_id.in_(closed),
            db.MessageDelivery.status.in_(["applied", "stale"]), db.MessageDelivery.result != {},
        ).order_by(db.MessageDelivery.id).limit(self.batch_size + 1)))
        for row in rows[:self.batch_size]:
            row.result = {}
        self.jobs.advance_in(job, phase="retained_guidance" if len(rows) > self.batch_size else "finalize")

    def _finalize(self, session, root, job):
        rows, more = self.details._run_batch(session, root.id, job.cursor)
        for row in rows:
            row.task = row.task if row.kind == "material" else row.task[:300]
            row.input_data = {}
            row.outcome = {key: row.details_summary.get(key, [] if key.endswith("_ids") else "")
                           for key in ("answer", "draft_ids", "published_ids")}
            row.outcome = {**row.outcome, **row.details_summary.get("business_outcome", {})}
            row.stop_reason = row.details_summary.get("stop_reason") or None
            row.worker_id, row.heartbeat_at, row.active_since = None, None, None
            row.lifecycle_state = "expired"
        # 根标记最后收束；否则中途失败会被后续步骤误认为非cleaning。
        root.lifecycle_state = "cleaning" if more else "expired"
        self.jobs.advance_in(job, phase="finalize" if more else "completed",
                             cursor={"after": rows[-1].id} if more else {}, completed=not more)
