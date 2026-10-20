"""文章删除：保留引用闭包中的固定版本，元数据先提交，精确文件清单随后回收。"""

import hashlib
import json

from sqlalchemy import delete, literal, select
from sqlalchemy.orm import defer

from knowledgeops_agent.app.lifecycle.asset_references import reachable_assets
from knowledgeops_agent.app.lifecycle.catalog import ReferenceCatalog
from knowledgeops_agent.app.lifecycle.files import _digest, _identity, _unlink
from knowledgeops_agent.app.lifecycle.jobs import job_view
from knowledgeops_agent.app.lifecycle.workspace import scan_workspace_references
from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.knowledge.files import workspace_lock
from knowledgeops_agent.persistence import models as db


class KnowledgeCleaner:
    PHASES = ("knowledge_collect", "knowledge_files", "knowledge_receipts", "knowledge_projection")

    def __init__(self, sessions, jobs, *, batch_size=100):
        self.sessions, self.jobs, self.batch_size = sessions, jobs, batch_size

    def _document(self, session, identity):
        row = session.get(db.KnowledgeDocument, _identity(identity))
        if row is None:
            raise KeyError("article not found")
        return row

    def _working_file(self, session, doc):
        store = session.info.get("file_store")
        if not store or not doc.workspace_path:
            return None
        key = doc.workspace_path
        if not key.startswith("knowledge/documents/") or ".." in key.split("/") or "\\" in key or not key.endswith(".md"):
            raise ValueError("invalid knowledge working path")
        path = store.path(key)
        if not path.exists():
            return None
        # 上限同知识工作树；不能将待同步的新编辑误当作已发布内容删除。
        if path.stat().st_size > 800000:
            raise ValueError("knowledge working file exceeds synchronization limit")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {"key": key, "sha256": digest, "working": True}

    def _versions(self, session, doc):
        workspace = scan_workspace_references(session.info.get("file_store"))
        if doc.workspace_path:
            workspace.pop(hashlib.sha256(doc.workspace_path.encode()).hexdigest(), None)
        graph = reachable_assets(session, excluding_knowledge=doc.id, workspace_values=workspace)
        return list(session.execute(select(db.KnowledgeVersion,
            (literal("asset:knowledge:") + db.KnowledgeVersion.id).in_(select(graph.c.node)).label("kept"))
            .options(defer(db.KnowledgeVersion._body))
            .where(db.KnowledgeVersion.document_id == doc.id).order_by(db.KnowledgeVersion.id)))

    def _preview(self, session, doc):
        blockers = []
        if doc.deleted_at:
            blockers.append("文章已从知识库移除。")
        if not ReferenceCatalog.ready(session):
            blockers.append("引用目录正在更新，请稍后重试。")
        versions = self._versions(session, doc)
        working = self._working_file(session, doc)
        current = session.get(db.KnowledgeVersion, doc.current_version_id) if doc.current_version_id else None
        if working and (current is None or working["sha256"] != hashlib.sha256(current.body.encode()).hexdigest()):
            blockers.append("工作文件存在未同步修改，请先保存或处理这些修改，再删除文章。")
        values = {"id": doc.id, "deleted": str(doc.deleted_at), "current": doc.current_version_id,
            "versions": [(v.id, v.body_sha256, kept) for v, kept in versions], "working": working, "blockers": blockers}
        return {"version": hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest(),
            "eligible": not blockers, "blockers": blockers, "title": current.title if current else "已删除文章",
            "counts": {"delete": sum(not kept for _, kept in versions), "keep": sum(kept for _, kept in versions)},
            "reasons": ["从知识库、检索和常规导出中移除文章，删除对应工作文件。",
                "聊天、其他文章、学习任务和有效草稿仍需使用的固定版本会保留，旧链接仍可查看；最后一处依赖解除后再回收。",
                "文章引用过的来源材料及独立交付文件不随文章删除。删除后不能直接恢复，请谨慎确认。"]}, working

    def preview(self, identity):
        with workspace_lock(self.sessions), self.sessions.begin() as session:
            lock_control(session)
            return self._preview(session, self._document(session, identity))[0]

    def admit(self, identity, *, expected_version):
        with workspace_lock(self.sessions), self.sessions.begin() as session:
            lock_control(session)
            doc = self._document(session, identity)
            existing = session.scalar(select(db.CleanupJob).where(
                db.CleanupJob.target_kind == "knowledge", db.CleanupJob.target_id == identity))
            if doc.deleted_at and existing:
                return job_view(existing)
            preview, working = self._preview(session, doc)
            if preview["version"] != expected_version:
                raise ValueError("文章或引用状态已变化，请重新核对删除范围。")
            if not preview["eligible"]:
                raise ValueError("；".join(preview["blockers"]))
            doc.deleted_at = self.jobs.now()
            session.execute(delete(db.SearchEntry).where(db.SearchEntry.document_id == identity))
            job = self.jobs.create(session, "knowledge", identity)
            self.jobs.advance_in(job, phase="knowledge_files", cursor={"files": [working] if working else []},
                summary={"title": preview["title"], "planned": preview["counts"], "deleted": 0, "kept": 0})
            return job_view(job)

    def step(self, *, identity=None):
        claim = self.jobs.claim(identity=identity, phases=self.PHASES)
        if claim is None:
            return None
        try:
            with workspace_lock(self.sessions):
                if claim["phase"] == "knowledge_projection":
                    from knowledgeops_agent.persistence.workspace import sync_knowledge_projection

                    sync_knowledge_projection(self.sessions)
                with self.sessions.begin() as session:
                    lock_control(session)
                    job = self.jobs.owned(session, claim["id"], claim["claim_token"])
                    doc = self._document(session, job.target_id)
                    if doc.deleted_at is None:
                        raise ValueError("article deletion has not been accepted")
                    if job.phase == "knowledge_files":
                        self._files(session, doc, job.cursor.get("files", []))
                        self.jobs.advance_in(job, phase="knowledge_collect")
                    elif job.phase == "knowledge_projection":
                        self.jobs.advance_in(job, phase="completed", completed=True)
                    elif job.phase == "knowledge_receipts":
                        self._receipts(session, doc, job)
                    else:
                        self._collect(session, doc, job)
        except Exception as error:
            self.jobs.fail(claim["id"], claim["claim_token"], error)
        return self.jobs.get(claim["id"])

    def _collect(self, session, doc, job):
        if not ReferenceCatalog.ready(session):
            raise ValueError("reference catalog is incomplete")
        versions = self._versions(session, doc)
        removed = [row for row, kept in versions if not kept][:self.batch_size]
        files = []
        for row in removed:
            if row.body_storage_key:
                expected = f"knowledge/versions/{_identity(row.id)}.md"
                if row.body_storage_key != expected or session.info.get("file_store") is None:
                    raise ValueError("invalid fixed knowledge storage")
                files.append({"key": expected, "sha256": _digest(row.body_sha256), "working": False})
            # 可达版本的引用已令此版本保留；剩余入边仅属于同样不可达的内容。
            evidence = select(db.Evidence.id).where(db.Evidence.knowledge_version_id == row.id)
            session.execute(delete(db.Citation).where(db.Citation.evidence_id.in_(evidence)))
            session.execute(delete(db.Evidence).where(db.Evidence.knowledge_version_id == row.id))
            session.execute(delete(db.Citation).where(db.Citation.version_id == row.id))
            session.execute(delete(db.KnowledgeRelation).where(db.KnowledgeRelation.version_id == row.id))
            session.execute(delete(db.ArtifactRetentionReference).where(
                db.ArtifactRetentionReference.owner_kind == "asset", db.ArtifactRetentionReference.owner_id == "knowledge:" + row.id))
            session.execute(delete(db.AssetReference).where(
                (db.AssetReference.origin_node == "asset:knowledge:" + row.id) |
                (db.AssetReference.target_node == "asset:knowledge:" + row.id)))
            if doc.current_version_id == row.id:
                doc.current_version_id = None
                session.flush()
            session.delete(row)
        self.jobs.advance_in(job, phase="knowledge_files" if removed else "knowledge_receipts",
            cursor={"files": files} if removed else {}, summary={**job.summary,
                "deleted": job.summary.get("deleted", 0) + len(removed), "kept": sum(kept for _, kept in versions)})

    def _files(self, session, doc, files):
        store = session.info.get("file_store")
        for item in files:
            key, digest = item["key"], _digest(item["sha256"])
            if item["working"]:
                if key != doc.workspace_path or not key.startswith("knowledge/documents/") or ".." in key.split("/"):
                    raise ValueError("working file does not belong to deleted article")
            else:
                version_id = key.removeprefix("knowledge/versions/").removesuffix(".md")
                if key != f"knowledge/versions/{_identity(version_id)}.md":
                    raise ValueError("fixed version path mismatch")
                if session.scalar(select(db.KnowledgeVersion.id).where(db.KnowledgeVersion.body_storage_key == key)):
                    continue
            if store is None:
                raise ValueError("knowledge cleanup requires its workspace")
            store.path(key)  # 先验证相对键，删除时仍逐级拒绝符号链接。
            _unlink(store, key, digest)

    def _receipt_candidates(self, session, doc, *, after=None, limit=100):
        graph = reachable_assets(session, workspace_values=scan_workspace_references(session.info.get("file_store")))
        query = select(db.ChangeSet).where(db.ChangeSet.status == "published",
            db.ChangeSet.operations.contains([{"document_id": doc.id}]),
            ~(literal("draft:") + db.ChangeSet.id).in_(select(graph.c.node)))
        if after:
            query = query.where(db.ChangeSet.id > after)
        return list(session.scalars(query.order_by(db.ChangeSet.id).limit(limit)))

    def _receipts(self, session, doc, job):
        """发布幂等身份不等于保留全部已删除正文；被实际引用的审阅修订仍保护。"""
        if not ReferenceCatalog.ready(session):
            raise ValueError("reference catalog is incomplete")
        rows = self._receipt_candidates(session, doc, after=job.cursor.get("after"), limit=self.batch_size + 1)
        compacted = 0
        for draft in rows[:self.batch_size]:
            identities = {item.get("document_id") for item in draft.operations}
            removed = set(session.scalars(select(db.KnowledgeDocument.id).where(
                db.KnowledgeDocument.id.in_(identities), db.KnowledgeDocument.deleted_at.is_not(None))))
            if not identities or identities != removed:
                continue  # 合并/拆分的同一回执仍涉及其他未删除文章，不能局部破坏审阅。
            session.execute(delete(db.ChangeSetRevision).where(db.ChangeSetRevision.changeset_id == draft.id))
            session.execute(delete(db.AssetReference).where(db.AssetReference.origin_node == "draft:" + draft.id))
            session.execute(delete(db.ArtifactRetentionReference).where(db.ArtifactRetentionReference.owner_kind == "draft",
                (db.ArtifactRetentionReference.owner_id == "current:" + draft.id) |
                db.ArtifactRetentionReference.owner_id.startswith(draft.id + ":")))
            draft.operations, draft.workspace_paths, draft.validated_evidence_ids = [], {}, []
            for receipt in session.scalars(select(db.OperationReceipt).where(db.OperationReceipt.result["id"].astext == draft.id)):
                receipt.result = {"id": draft.id, "status": "published", "revision": draft.revision,
                                  "operations": [], "content_removed": True}
            compacted += 1
        more = len(rows) > self.batch_size
        self.jobs.advance_in(job, phase="knowledge_receipts" if more else "knowledge_projection",
            cursor={"after": rows[self.batch_size - 1].id} if more else {},
            summary={**job.summary, "receipts_compacted": job.summary.get("receipts_compacted", 0) + compacted})

    def reconcile(self, *, after=None, limit=100):
        """最后一处依赖解除后重新排队；保留最小删除身份，不让旧请求复活文章。"""
        with workspace_lock(self.sessions), self.sessions.begin() as session:
            lock_control(session)
            if not ReferenceCatalog.ready(session):
                return {"next_cursor": after, "waiting_for_references": True}
            query = select(db.KnowledgeDocument).where(db.KnowledgeDocument.deleted_at.is_not(None))
            if after:
                query = query.where(db.KnowledgeDocument.id > after)
            rows = list(session.scalars(query.order_by(db.KnowledgeDocument.id).limit(limit + 1)))
            for doc in rows[:limit]:
                existing = session.scalar(select(db.CleanupJob).where(
                    db.CleanupJob.target_kind == "knowledge", db.CleanupJob.target_id == doc.id))
                if existing and existing.status != "completed":
                    continue
                if any(not kept for _, kept in self._versions(session, doc)) or self._receipt_candidates(session, doc, limit=1):
                    job = existing or self.jobs.create(session, "knowledge", doc.id)
                    self.jobs.advance_in(job, phase="knowledge_collect", summary={**job.summary,
                        "title": job.summary.get("title", "已删除文章"), "deleted": 0, "kept": 0})
            return {"next_cursor": rows[limit - 1].id if len(rows) > limit else None}
