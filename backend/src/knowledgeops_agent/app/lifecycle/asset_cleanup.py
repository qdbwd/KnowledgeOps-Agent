"""单份资产的明确移除与可续跑回收；共享正文按依赖保留，工作文件不随交付删除。"""

import fcntl
import hashlib
import json
import os
import stat

from sqlalchemy import delete, func, literal, or_, select, union

from knowledgeops_agent.app.lifecycle.asset_references import reachable_assets
from knowledgeops_agent.app.lifecycle.assets import (
    _remove_version,
    collect_source_files,
    remove_draft,
)
from knowledgeops_agent.app.lifecycle.catalog import ReferenceCatalog
from knowledgeops_agent.app.lifecycle.files import _digest, _identity, _unlink
from knowledgeops_agent.app.lifecycle.jobs import job_view
from knowledgeops_agent.app.lifecycle.service import SETTLED
from knowledgeops_agent.app.lifecycle.workspace import scan_workspace_references
from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.knowledge.files import workspace_lock
from knowledgeops_agent.persistence import models as db


def public_job(row):
    """产品只读取进度和影响，不暴露租约令牌或物理文件删除清单。"""
    return {key: row[key] for key in ("id", "target_kind", "target_id", "status", "phase", "summary",
                                     "last_error", "created_at", "updated_at", "finished_at")}


class AssetCleaner:
    PHASES = ("asset_collect", "asset_files")

    def __init__(self, sessions, jobs, *, batch_size=100, eligibility=None):
        self.sessions, self.jobs, self.batch_size = sessions, jobs, batch_size
        self.eligibility = eligibility

    @staticmethod
    def target(kind, identity, conversation_id=None):
        if kind not in {"source", "delivery"} or (kind == "delivery" and conversation_id):
            raise ValueError("unsupported asset deletion scope")
        _identity(identity)
        return identity + (":" + _identity(conversation_id) if conversation_id else "")

    @staticmethod
    def _owner(conversation_id):
        return ("conversation", conversation_id) if conversation_id else ("library", "default")

    def _preview(self, session, kind, identity, conversation_id=None):
        self.target(kind, identity, conversation_id)
        row = session.get(db.Source if kind == "source" else db.DeliveredFile, identity)
        if row is None:
            raise KeyError("asset not found")
        blockers: list[str] = []
        reasons: list[str] = []
        state: list[object] = []
        if not ReferenceCatalog.ready(session):
            blockers.append("正在核对已有内容的引用关系，请稍后重新预览。")
        values = scan_workspace_references(session.info.get("file_store"))
        owner = self._owner(conversation_id)
        graph = reachable_assets(session, excluding_source=identity if kind == "source" else None,
            excluding_source_owner=owner, excluding_delivery=identity if kind == "delivery" else None,
            workspace_values=values)
        nodes = select(graph.c.node)
        if kind == "source":
            uses = list(session.scalars(select(db.SourceUse).where(db.SourceUse.source_id == identity)
                                       .order_by(db.SourceUse.owner_kind, db.SourceUse.owner_id)))
            owns = any((use.owner_kind, use.owner_id) == owner for use in uses)
            if not owns and not (not row.ownership_known and owner[0] == "library"):
                blockers.append("材料已不在这个列表中，请刷新页面。")
            if conversation_id:
                chat = session.get(db.Conversation, conversation_id)
                if chat is None or chat.deletion_state != "active":
                    blockers.append("会话已关闭，不能单独修改它的材料。")
            state = [(use.owner_kind, use.owner_id, use.created_at.isoformat()) for use in uses]
            state += [row.ownership_known, row.deleted_at.isoformat() if row.deleted_at else None]
            query = select(db.SourceVersion.id, db.SourceVersion.content_hash, db.SourceVersion.status,
                (literal("source_version:") + db.SourceVersion.id).in_(nodes).label("kept"))\
                .where(db.SourceVersion.source_id == identity).order_by(db.SourceVersion.id)
            title = row.title
        else:
            if row.deleted_at is not None:
                blockers.append("文件已从交付列表移除，请刷新页面。")
            query = select(db.DeliveredFile.id, db.DeliveredFile.sha256, literal("ready"),
                (literal("delivery:") + db.DeliveredFile.id).in_(nodes).label("kept"))\
                .where(db.DeliveredFile.id == identity)
            title = row.name
        # 会话附件在未决任务中可能尚未被模型实际读取；不能趁工具目录未形成引用时删除。
        runs = select(db.Run).where(db.Run.parent_id.is_(None), db.Run.lifecycle_state == "active")
        if conversation_id:
            runs = runs.where(db.Run.conversation_id == conversation_id)
        elif kind == "delivery":
            runs = runs.where(db.Run.id == row.root_id)
        else:
            runs = runs.where(literal(False))
        for root in session.scalars(runs).yield_per(100):
            pending = self.eligibility._dependencies(session, root)[0] if self.eligibility else root.status not in SETTLED
            if pending:
                blockers.append("所属任务仍在执行、等待或需要核对，请先处理该任务。")
                break
        counts = {"delete": 0, "keep": 0}
        digest = hashlib.sha256(json.dumps([kind, identity, owner, state], sort_keys=True).encode())
        for version in session.execute(query).yield_per(100):
            keep = version.kept or bool(blockers)
            counts["keep" if keep else "delete"] += 1
            digest.update(json.dumps(list(version)).encode())
            if version[2] not in {"ready", "failed", "partial"}:
                blockers.append("材料仍在解析，请等待处理完成后再删除。")
        if counts["keep"] and not reasons:
            reasons.append("已有会话、独立资产、固定引用或工作文件仍在使用；对应内容保持可读。")
        digest.update(json.dumps(sorted(set(blockers))).encode())
        return {"kind": kind, "identity": identity, "title": title, "version": digest.hexdigest(),
                "eligible": not blockers, "blockers": sorted(set(blockers)), "counts": counts,
                "reasons": reasons, "scope": "conversation" if conversation_id else "library"}

    def preview(self, kind, identity, *, conversation_id=None):
        with workspace_lock(self.sessions), self.sessions.begin() as session:
            lock_control(session)
            return self._preview(session, kind, identity, conversation_id)

    def admit(self, kind, identity, *, expected_version, conversation_id=None):
        """只解除当前入口的保存关系；与清理意图同事务，不直接删文件。"""
        target = self.target(kind, identity, conversation_id)
        with workspace_lock(self.sessions), self.sessions.begin() as session:
            lock_control(session)
            existing = session.scalar(select(db.CleanupJob).where(db.CleanupJob.target_kind == kind,
                                                                db.CleanupJob.target_id == target))
            if existing and existing.status != "completed":
                return job_view(existing)
            row = session.get(db.Source if kind == "source" else db.DeliveredFile, identity)
            if existing and (row is None or row.deleted_at is not None):
                return job_view(existing)
            preview = self._preview(session, kind, identity, conversation_id)
            if preview["version"] != expected_version:
                raise ValueError("材料或引用状态已变化，请重新预览后删除。")
            if not preview["eligible"]:
                raise ValueError("；".join(preview["blockers"]))
            if kind == "source":
                owner = self._owner(conversation_id)
                session.execute(delete(db.SourceUse).where(db.SourceUse.source_id == identity,
                    db.SourceUse.owner_kind == owner[0], db.SourceUse.owner_id == owner[1]))
                if owner[0] == "library":
                    row.ownership_known = True
                if not session.scalar(select(db.SourceUse.source_id).where(db.SourceUse.source_id == identity).limit(1)):
                    row.deleted_at = self.jobs.now()
                    row.identity_key = f"deleted:{row.id}:{row.identity_key}"
            else:
                row.deleted_at = self.jobs.now()
            job = existing or self.jobs.create(session, kind, target)
            self.jobs.advance_in(job, phase="asset_collect", summary={"title": preview["title"],
                "scope": preview["scope"], "planned": preview["counts"], "reasons": preview["reasons"],
                "deleted": 0, "kept": 0})
            return job_view(job)

    def list_jobs(self, *, limit=20, kind=None):
        with self.sessions() as session:
            from sqlalchemy import and_, or_

            query = select(db.CleanupJob).where(or_(
                db.CleanupJob.target_kind.in_(["source", "delivery", "draft", "knowledge"]),
                and_(db.CleanupJob.target_kind == "run", db.CleanupJob.summary["user_requested"].as_boolean().is_(True))))
            if kind:
                query = query.where(db.CleanupJob.target_kind == kind)
            else:
                query = query.where(db.CleanupJob.target_kind.in_(["source", "delivery", "draft"]))
            return [public_job(job_view(row)) for row in session.scalars(query.order_by(
                (db.CleanupJob.status == "completed").asc(), db.CleanupJob.updated_at.desc()).limit(limit))]

    def get_job(self, identity):
        job = self.jobs.get(identity)
        if job["target_kind"] not in {"source", "delivery", "draft", "knowledge", "run"} or (
            job["target_kind"] == "run" and not job["summary"].get("user_requested")
        ):
            raise KeyError("asset deletion job not found")
        return public_job(job)

    def retry(self, identity):
        self.get_job(identity)
        return public_job(self.jobs.retry(identity))

    def step(self, *, identity=None):
        claim = self.jobs.claim(identity=identity, phases=self.PHASES)
        if claim is None:
            return None
        try:
            with workspace_lock(self.sessions), self.sessions.begin() as session:
                lock_control(session)
                job = self.jobs.owned(session, claim["id"], claim["claim_token"])
                if job.phase == "asset_files":
                    if job.target_kind == "source":
                        collect_source_files(session, job.cursor["files"])
                    else:
                        self._delivery_files(session, job.cursor["files"])
                    self.jobs.advance_in(job, phase="asset_collect")
                else:
                    self._collect(session, job)
        except Exception as error:
            self.jobs.fail(claim["id"], claim["claim_token"], error)
        return self.get_job(claim["id"])

    def _collect(self, session, job):
        if not ReferenceCatalog.ready(session):
            self.jobs.advance_in(job, phase="asset_collect")
            return
        identity = job.target_id.split(":", 1)[0]
        graph = reachable_assets(session, workspace_values=scan_workspace_references(session.info.get("file_store")))
        nodes = select(graph.c.node)
        files, removed = [], 0
        if job.target_kind == "source":
            query = select(db.SourceVersion).where(db.SourceVersion.source_id == identity)
            versions = list(session.scalars(query.where(~(literal("source_version:") + db.SourceVersion.id).in_(nodes))
                .order_by(db.SourceVersion.id).limit(self.batch_size)))
            for version in versions:
                if original := _remove_version(session, version):
                    files.append(original)
            removed = len(versions)
            kept = session.scalar(select(func.count()).select_from(query.subquery())) if not removed else 0
        elif job.target_kind == "draft":
            row = session.get(db.ChangeSet, identity)
            owner = session.get(db.Run, row.root_id) if row else None
            kept = int(bool(row and (not owner or owner.lifecycle_state not in {"cleaning", "expired"}
                or row.retention_intent != "temporary" or row.status == "published" or session.scalar(
                select(graph.c.node).where(graph.c.node == "draft:" + identity)))))
            if row and not kept:
                remove_draft(session, row)
                removed = 1
        else:
            row = session.get(db.DeliveredFile, identity)
            kept = int(bool(row and session.scalar(select(graph.c.node).where(graph.c.node == "delivery:" + identity))))
            if row and not kept:
                if row.storage_key != f"deliveries/blobs/{_digest(row.sha256)[:2]}/{row.sha256}":
                    raise ValueError("delivery storage key is outside its content-addressed storage")
                files = [[row.storage_key, row.sha256]]
                session.execute(delete(db.AssetReference).where(or_(db.AssetReference.origin_node == "delivery:" + identity,
                    db.AssetReference.target_node == "delivery:" + identity)))
                session.delete(row)
                removed = 1
        summary = {**job.summary, "deleted": job.summary.get("deleted", 0) + removed, "kept": kept}
        self.jobs.advance_in(job, phase="asset_files" if files else "asset_collect" if removed else "completed",
            cursor={"files": files} if files else {}, summary=summary, completed=not files and not removed)

    def reconcile(self, *, after=None, limit=100):
        """日常有界巡检：最后一个真实引用消失后重新安排回收，不把首次保留当永久钉住。

        旧意图未知的来源仍是根，不会进入候选。这里只登记作业，不在扫描事务删文件。
        """
        with workspace_lock(self.sessions), self.sessions.begin() as session:
            lock_control(session)
            if not ReferenceCatalog.ready(session):
                return {"scheduled": 0, "next_cursor": after, "waiting_for_references": True}
            graph = reachable_assets(session, workspace_values=scan_workspace_references(session.info.get("file_store")))
            nodes = select(graph.c.node)
            candidates = union(select((literal("source:") + db.Source.id).label("node")).join(db.SourceVersion).where(
                db.Source.ownership_known, ~(literal("source_version:") + db.SourceVersion.id).in_(nodes)),
                select((literal("delivery:") + db.DeliveredFile.id).label("node")).where(
                    db.DeliveredFile.deleted_at.is_not(None), ~(literal("delivery:") + db.DeliveredFile.id).in_(nodes)),
                select((literal("draft:") + db.ChangeSet.id).label("node")).where(
                    db.ChangeSet.retention_intent == "temporary", db.ChangeSet.status != "published", ~(literal("draft:") + db.ChangeSet.id).in_(nodes),
                    db.ChangeSet.root_id.in_(select(db.Run.id).where(db.Run.lifecycle_state.in_(["cleaning", "expired"]))),
                )).subquery()
            query = select(candidates.c.node).order_by(candidates.c.node).limit(limit + 1)
            if after:
                query = query.where(candidates.c.node > after)
            found = list(session.scalars(query))
            scheduled = 0
            for node in found[:limit]:
                kind, identity = node.split(":", 1)
                # 人工移除和另一轮扫描已安排的作业优先；同来源不同会话也不重复领取。
                if session.scalar(select(db.CleanupJob.id).where(db.CleanupJob.target_kind == kind,
                    or_(db.CleanupJob.target_id == identity, db.CleanupJob.target_id.startswith(identity + ":")),
                    db.CleanupJob.status != "completed").limit(1)):
                    continue
                row = session.get({"source": db.Source, "delivery": db.DeliveredFile, "draft": db.ChangeSet}[kind], identity)
                job = self.jobs.create(session, kind, identity)
                title = row.title if kind == "source" else row.name if kind == "delivery" else "临时知识草稿"
                self.jobs.advance_in(job, phase="asset_collect", summary={"title": title,
                    "scope": "unreferenced", "reasons": ["最后一个有效使用者已解除。"], "deleted": 0, "kept": 0})
                scheduled += 1
            return {"scheduled": scheduled, "next_cursor": found[limit - 1] if len(found) > limit else None}

    @staticmethod
    def _delivery_files(session, files):
        """与交付登记的共享锁配合，非阻塞独占锁避免控制锁与文件锁顺序反转。"""
        store = session.info.get("file_store")
        if store is None:
            raise ValueError("delivery cleanup requires the configured workspace")
        for key, digest in files:
            if key != f"deliveries/blobs/{_digest(digest)[:2]}/{digest}":
                raise ValueError("invalid delivery cleanup manifest")
            try:
                target = store.path(key)
                descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                continue
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("delivery cleanup only removes regular files")
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                current = target.stat()
                if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                    raise ValueError("delivery file changed during cleanup")
                if not session.scalar(select(db.DeliveredFile.id).where(db.DeliveredFile.storage_key == key).limit(1)):
                    _unlink(store, key, digest)
