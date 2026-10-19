"""历史网页原件的显式整理：只读影响报告 → 恢复证据 → 解除关联 → 精确回收。

不在启动/迁移中自动接收任务；保留原正文、片段、Evidence和content_hash身份。
预览兼容0030旧表，实际执行必须先完成计划内结构迁移。
"""

import hashlib
import json
import os
import stat
from contextlib import ExitStack

from sqlalchemy import select, text

from knowledgeops_agent.app.lifecycle.assets import collect_source_files
from knowledgeops_agent.app.lifecycle.files import _digest
from knowledgeops_agent.app.lifecycle.jobs import CleanupJobs, job_view
from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.persistence import models as db
from knowledgeops_agent.persistence.backup import verified_restore


def _original(root, key, digest):
    if key != f"{_digest(digest)[:2]}/{digest}":
        raise ValueError("原件路径不符合内容寻址规则")
    with ExitStack() as stack:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, directory)
        directory = os.open(key[:2], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        stack.callback(os.close, directory)
        descriptor = os.open(digest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("原件不是普通文件")
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("原件哈希不一致")
            after = os.stat(digest, dir_fd=directory, follow_symlinks=False)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
            ):
                raise ValueError("原件在检查期间发生变化")
            return before.st_size


def inspect_version(session, store, identity):
    # to_jsonb只读取旧库实际存在的列，预览不因新版ORM列缺失而要求先改正式库。
    row = session.execute(text("""SELECT to_jsonb(s) AS source, to_jsonb(v) AS version
        FROM sources s JOIN source_versions v ON v.source_id=s.id WHERE v.id=:id"""), {"id": identity}).mappings().first()
    if row is None:
        raise KeyError("source version not found")
    source, version = row["source"], row["version"]
    result = {"version_id": identity, "source_id": source["id"], "title": source["title"],
              "url": source["origin"], "number": version["number"], "storage_key": version["storage_key"],
              "eligible": False, "reason": None, "original_bytes": 0}
    try:
        if source["kind"] != "web" or not version["storage_key"]:
            raise ValueError("不是保留HTML原件的网页版本")
        if version["mime_type"].split(";", 1)[0].strip().lower() not in {"text/html", "application/xhtml+xml"}:
            raise ValueError("原件类型不是HTML，需要人工核对")
        if version["status"] != "ready":
            raise ValueError("正文尚未完整提取")
        segments = list(session.scalars(select(db.SourceSegment).where(db.SourceSegment.source_version_id == identity)
                                       .order_by(db.SourceSegment.position)))
        position, offset, pieces = 0, 0, []
        for segment in segments:
            if (segment.status != "ready" or segment.error or segment.page is not None or segment.position != position
                or segment.start_offset != offset or segment.end_offset != offset + len(segment.text)):
                raise ValueError("正文片段不连续或提取状态异常")
            position, offset = position + 1, segment.end_offset
            pieces.append(segment.text)
        body = "".join(pieces)
        if not body.strip():
            raise ValueError("没有可保留的已提取正文")
        body_hash = hashlib.sha256(body.encode()).hexdigest()
        if version.get("body_hash") and body_hash != version["body_hash"]:
            raise ValueError("正文哈希不一致")
        segments_by_id = {segment.id: segment for segment in segments}
        evidence_hash = hashlib.sha256()
        evidence_count = 0
        for evidence in session.scalars(select(db.Evidence).where(db.Evidence.source_segment_id.in_(segments_by_id))
                                       .order_by(db.Evidence.id)):
            segment = segments_by_id[evidence.source_segment_id]
            start, end = evidence.start_offset, evidence.end_offset
            if not segment.start_offset <= start <= end <= segment.end_offset or body[start:end] != evidence.quote:
                raise ValueError("已有引用与保留正文不一致")
            evidence_hash.update(json.dumps([evidence.id, evidence.source_segment_id, start, end, evidence.quote]).encode())
            evidence_count += 1
        digest = version.get("original_hash") or version["content_hash"]
        size = _original(store.root, version["storage_key"], digest)
        shared = list(session.execute(text("SELECT id FROM source_versions WHERE storage_key=:key AND id<>:id ORDER BY id"),
                                      {"key": version["storage_key"], "id": identity}).scalars())
        result.update(eligible=True, original_bytes=size, original_hash=digest, body_hash=body_hash,
                      body_characters=len(body), evidence_count=evidence_count, shared_with=shared,
                      evidence_fingerprint=evidence_hash.hexdigest(), content_hash=version["content_hash"],
                      parser=version["parser"], body_format=version.get("body_format") or "text")
    except (ValueError, OSError) as error:
        result["reason"] = str(error) if isinstance(error, ValueError) else type(error).__name__
    # 共享者在同批其他版本解除关联时会变化；物理阶段重新查询，不能让正常批次自相失效。
    stable = {key: value for key, value in result.items() if key != "shared_with"}
    result["fingerprint"] = hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result


class LegacyHtmlCleaner:
    PHASES = ("html_detach", "html_files")

    def __init__(self, sessions, jobs=None):
        self.sessions, self.jobs = sessions, jobs or CleanupJobs(sessions)

    def preview(self, *, after=None, limit=100):
        if not 1 <= limit <= 500:
            raise ValueError("HTML inspection batch must be between 1 and 500")
        with self.sessions() as session:
            store = session.info["source_store"]
            ids = list(session.execute(text("""SELECT v.id FROM source_versions v JOIN sources s ON s.id=v.source_id
                WHERE s.kind='web' AND v.storage_key IS NOT NULL AND (CAST(:after AS text) IS NULL OR v.id>:after)
                ORDER BY v.id LIMIT :limit"""), {"after": after, "limit": limit + 1}).scalars())
            return {"mode": "preview", "database": session.get_bind().engine.url.database,
                    "items": [inspect_version(session, store, identity) for identity in ids[:limit]],
                    "next_cursor": ids[limit - 1] if len(ids) > limit else None}

    def admit(self, approved_item, *, backup_folder):
        """接受精确版本及预览指纹；无匹配恢复证据不创建清理意图。"""
        database = self.sessions.kw["bind"].url.database
        backup = verified_restore(backup_folder, source_database=database)
        identity = approved_item["version_id"]
        with self.sessions.begin() as session:
            lock_control(session)
            prior = session.scalar(select(db.CleanupJob).where(db.CleanupJob.target_kind == "html_original",
                                                              db.CleanupJob.target_id == identity))
            if prior is not None:
                if prior.summary.get("approved_fingerprint") != approved_item["fingerprint"]:
                    raise ValueError("该版本已经有另一份整理作业，请核对原作业")
                return job_view(prior)
            item = inspect_version(session, session.info["source_store"], identity)
            if not item["eligible"] or item["fingerprint"] != approved_item["fingerprint"]:
                raise ValueError("HTML整理预览已变化，请重新核对")
            body = next((row for row in backup.get("bodies", []) if row["version_id"] == identity), None)
            original = next((row for row in backup.get("files", []) if row["version_id"] == identity), None)
            if (not body or body["sha256"] != item["body_hash"] or not original
                or not original["matches"] or original["sha256"] != item["original_hash"]):
                raise ValueError("恢复后的正文和原件与当前版本不匹配")
            # ORM的新增列在这里才使用；预览可以先于正式结构迁移独立执行。
            session.get(db.SourceVersion, identity)
            job = self.jobs.create(session, "html_original", identity)
            self.jobs.advance_in(job, phase="html_detach", cursor={"approved": item}, summary={
                "title": item["title"], "version_id": identity, "approved_fingerprint": item["fingerprint"],
                "backup_manifest_sha256": backup["backup_manifest_sha256"], "original_bytes": item["original_bytes"],
            })
            return job_view(job)

    def step(self, *, identity):
        claim = self.jobs.claim(identity=identity, phases=self.PHASES)
        if claim is None:
            return None
        try:
            with self.sessions.begin() as session:
                lock_control(session)
                job = self.jobs.owned(session, claim["id"], claim["claim_token"])
                if job.phase == "html_detach":
                    approved = job.cursor["approved"]
                    item = inspect_version(session, session.info["source_store"], job.target_id)
                    if not item["eligible"] or item["fingerprint"] != approved["fingerprint"]:
                        raise ValueError("HTML整理对象在执行前发生变化")
                    version = session.get(db.SourceVersion, job.target_id)
                    version.body_hash = item["body_hash"]
                    version.body_format = item["body_format"]
                    version.original_hash = item["original_hash"]
                    version.storage_key = None
                    self.jobs.advance_in(job, phase="html_files", cursor={"files": [[item["storage_key"], item["original_hash"]]]})
                else:
                    key = job.cursor["files"][0][0]
                    shared = session.scalar(select(db.SourceVersion.id).where(db.SourceVersion.storage_key == key).limit(1))
                    collect_source_files(session, job.cursor["files"])
                    self.jobs.advance_in(job, phase="completed", completed=True, summary={**job.summary,
                        "shared_file_retained": bool(shared), "body_preserved": True})
        except Exception as error:
            self.jobs.fail(claim["id"], claim["claim_token"], error)
        return self.jobs.get(claim["id"])
