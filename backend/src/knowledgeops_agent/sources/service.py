"""原始材料、不可变解析版本与真实读取证据。

文件名从内容哈希生成，显示标题不会拼进路径。证据由读取操作生成而非模型
自报，后续正文只引用可解析的证据 ID；网页/PDF 适配器复用此存储边界。
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert

from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.persistence.models import (
    Evidence,
    KnowledgeVersion,
    Source,
    SourceSegment,
    SourceVersion,
    new_id,
    utcnow,
)
from knowledgeops_agent.sources.ownership import (
    claim_source,
    ownership_view,
    resolve_owner,
    scope_predicate,
)


class SourceService:
    def __init__(self, sessions, storage_root: Path):
        self.sessions = sessions
        self.storage_root = storage_root.resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)

    def _store_original(self, data: bytes, *, session) -> tuple[str, str]:
        """先原子保存不可变原件；DB 回滚最多留下可回收孤立对象，不留下假来源。"""
        digest = hashlib.sha256(data).hexdigest()
        key = f"{digest[:2]}/{digest}"
        FileStore(self.storage_root).put_bytes(key, data, session=session, purpose="source")
        return digest, key

    def import_text(
        self,
        title: str,
        content: str,
        *,
        kind: str = "text",
        origin: str | None = None,
        raw_bytes: bytes | None = None,
        mime_type: str = "text/plain",
        parser: str = "text-v1",
        body_format: str | None = None,
        conversation_id: str | None = None,
        root_id: str | None = None,
    ) -> dict:
        """导入文本并按 2400 字符建立可定位片段；相同来源/内容不重复版本。

        origin 存在时识别同一外部来源，否则以内容哈希识别独立粘贴材料。
        网页只保存已提取正文，按正文、格式及解析器去重；上传文件仍保存原件。
        原有版本的内容哈希语义与ID不变，新网页的去重指纹与两种校验哈希分开。
        """
        if not title.strip() or len(title) > 500 or not content.strip():
            raise ValueError("nonempty title (<=500 characters) and content are required")
        body_format = body_format or (
            "text" if kind == "file" and not title.lower().endswith(".md") else "markdown"
        )
        if body_format not in {"text", "markdown"}:
            raise ValueError("body_format must be text or markdown")
        body_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        original_hash, key = None, None
        if kind == "web":
            hash_kind = "body-v1"
            digest = hashlib.sha256(
                f"web-body-v1\0{parser}\0{body_format}\0{body_hash}".encode()
            ).hexdigest()
        else:
            hash_kind = "original"
            digest = hashlib.sha256(raw_bytes if raw_bytes is not None else content.encode("utf-8")).hexdigest()
            original_hash = digest
        identity = f"{kind}:{origin or (body_hash if kind == 'web' else digest)}"
        with self.sessions.begin() as session:
            lock_control(session)
            resolve_owner(session, conversation_id=conversation_id, root_id=root_id)
            session.execute(
                insert(Source)
                .values(id=new_id(), identity_key=identity, title=title, kind=kind, origin=origin, ownership_known=True)
                .on_conflict_do_nothing(index_elements=["identity_key"])
            )
            source = session.scalar(
                select(Source).where(Source.identity_key == identity).with_for_update()
            )
            claim_source(session, source.id, conversation_id=conversation_id, root_id=root_id)
            if kind != "web":
                # 保存意图通过后才安装原件；安装到版本提交期间与回收共用控制锁。
                _, key = self._store_original(raw_bytes if raw_bytes is not None else content.encode("utf-8"), session=session)
            version = session.scalar(
                select(SourceVersion).where(
                    SourceVersion.source_id == source.id, SourceVersion.content_hash == digest
                )
            )
            if version is None:
                number = (
                    session.scalar(
                        select(func.coalesce(func.max(SourceVersion.number), 0))
                        .select_from(SourceVersion)
                        .where(SourceVersion.source_id == source.id)
                    )
                    + 1
                )
                version = SourceVersion(
                    source_id=source.id,
                    number=number,
                    content_hash=digest,
                    content_hash_kind=hash_kind,
                    body_hash=body_hash,
                    body_format=body_format,
                    original_hash=original_hash,
                    storage_key=key,
                    mime_type=mime_type,
                    status="ready",
                    parser=parser,
                )
                session.add(version)
                session.flush()
                for position, offset in enumerate(range(0, len(content), 2400)):
                    piece = content[offset : offset + 2400]
                    session.add(
                        SourceSegment(
                            source_version_id=version.id,
                            position=position,
                            start_offset=offset,
                            end_offset=offset + len(piece),
                            text=piece,
                            method="text",
                            status="ready",
                        )
                    )
                session.flush()
            return self._version_view(session, source, version)

    def register_pdf(self, title: str, data: bytes, *, conversation_id: str | None = None,
                     root_id: str | None = None) -> dict:
        """上传阶段只保存原件与待解析版本；耗时的文本/OCR 工作留给 worker。"""
        if not data.startswith(b"%PDF-") or not title.strip() or len(data) > 10 * 1024 * 1024:
            raise ValueError("PDF must have a valid header and be at most 10 MiB")
        digest = hashlib.sha256(data).hexdigest()
        identity = f"pdf:{digest}"
        with self.sessions.begin() as session:
            lock_control(session)
            resolve_owner(session, conversation_id=conversation_id, root_id=root_id)
            session.execute(
                insert(Source)
                .values(id=new_id(), identity_key=identity, title=title[:500], kind="pdf", ownership_known=True)
                .on_conflict_do_nothing(index_elements=["identity_key"])
            )
            source = session.scalar(
                select(Source).where(Source.identity_key == identity).with_for_update()
            )
            claim_source(session, source.id, conversation_id=conversation_id, root_id=root_id)
            _, key = self._store_original(data, session=session)
            version = session.scalar(
                select(SourceVersion).where(SourceVersion.source_id == source.id)
            )
            if version is None:
                version = SourceVersion(
                    source_id=source.id,
                    number=1,
                    content_hash=digest,
                    original_hash=digest,
                    body_format="text",
                    storage_key=key,
                    mime_type="application/pdf",
                    status="pending",
                    parser="pypdf+tesseract-v1",
                )
                session.add(version)
                session.flush()
            return self._version_view(session, source, version)

    def get_version(self, version_id: str) -> dict:
        """通过不可变版本 ID 定位来源，不追随当前版本指针。"""
        with self.sessions() as session:
            version = session.get(SourceVersion, version_id)
            if version is None:
                raise KeyError("source version not found")
            return self._version_view(session, session.get(Source, version.source_id), version)

    def _version_view(self, session, source, version) -> dict:
        segments = session.scalars(
            select(SourceSegment)
            .where(SourceSegment.source_version_id == version.id)
            .order_by(SourceSegment.page.asc().nulls_first(), SourceSegment.position)
        ).all()
        return {
            "id": source.id,
            "title": source.title,
            "kind": source.kind,
            "origin": source.origin,
            **ownership_view(session, source),
            "deleted": source.deleted_at is not None,
            "version_id": version.id,
            "version": version.number,
            "status": version.status,
            "error": version.error,
            "content_hash": version.content_hash,
            "content_hash_kind": version.content_hash_kind,
            "body_hash": version.body_hash,
            "body_format": version.body_format or (
                "text" if source.kind in {"pdf", "file"} and not source.title.lower().endswith(".md")
                else "markdown"
            ),
            "original_available": source.kind != "web" and bool(version.storage_key),
            "mime_type": version.mime_type,
            "parser": version.parser,
            "captured_at": version.created_at.isoformat(),
            "segments": [
                {
                    "id": item.id,
                    "position": item.position,
                    "page": item.page,
                    "start": item.start_offset,
                    "end": item.end_offset,
                    "status": item.status,
                    "method": item.method,
                    "error": item.error,
                }
                for item in segments
            ],
        }

    def get_source(self, source_id: str, *, version_id: str | None = None, allow_removed: bool = False) -> dict:
        with self.sessions() as session:
            source = session.get(Source, source_id)
            if source is None or (source.deleted_at is not None and version_id is None and not allow_removed):
                raise KeyError("source not found")
            query = select(SourceVersion).where(SourceVersion.source_id == source_id)
            if version_id:
                query = query.where(SourceVersion.id == version_id)
            version = session.scalar(query.order_by(SourceVersion.number.desc()).limit(1))
            if version is None:
                raise KeyError("source version not found")
            return self._version_view(session, source, version)

    def list_sources(self, *, conversation_id: str | None = None, root_id: str | None = None,
                     include_library: bool = True) -> list[dict]:
        """默认列出独立材料及归属未知的旧材料；聊天入口再合并本会话材料。"""
        with self.sessions() as session:
            query = select(Source).where(Source.deleted_at.is_(None), scope_predicate(
                session, conversation_id=conversation_id, root_id=root_id, include_library=include_library,
            )).order_by(Source.created_at.desc())
            return [
                {
                    "id": source.id,
                    "title": source.title,
                    "kind": source.kind,
                    "origin": source.origin,
                    **ownership_view(session, source),
                }
                for source in session.scalars(query)
            ]

    def retain_source(self, source_id: str, *, conversation_id: str | None = None,
                      root_id: str | None = None) -> dict:
        """复用缓存也登记新使用者；不通过重新提取/复制来表示归属。"""
        with self.sessions.begin() as session:
            claim_source(session, source_id, conversation_id=conversation_id, root_id=root_id)
        return self.get_source(source_id)

    def save_source(self, source_id: str) -> dict:
        """用户明确保存到材料库；这是独立保留选择，不修改固定正文与旧引用。"""
        with self.sessions.begin() as session:
            claim_source(session, source_id)
            session.get(Source, source_id).ownership_known = True
        return self.get_source(source_id)

    def remove_source(self, source_id: str, *, expected_version: str, conversation_id: str | None = None) -> dict:
        """通过已预览的明确删除意图进入统一作业，不再保留另一条仅隐藏列表的路径。"""
        from knowledgeops_agent.app.lifecycle.asset_cleanup import AssetCleaner
        from knowledgeops_agent.app.lifecycle.jobs import CleanupJobs

        return AssetCleaner(self.sessions, CleanupJobs(self.sessions)).admit(
            "source", source_id, expected_version=expected_version, conversation_id=conversation_id,
        )

    def full_content(self, version_id: str) -> dict:
        """浏览器按固定版本读取全部解析正文，不新增 Agent 阅读证据。

        普通文本按偏移直接拼回原文；另返回页面数组供PDF阅读器区分失败页。
        新版本在展示前核对完整正文哈希；旧版本未完成校验迁移时明确标记。
        """
        with self.sessions() as session:
            version = session.get(SourceVersion, version_id)
            if version is None:
                raise KeyError("source version not found")
            segments, text = self._verified_body(session, version)
            return {"version_id": version_id, "text": text, "body_hash": version.body_hash,
                    "integrity": "verified" if version.body_hash else (
                        "incomplete" if version.status != "ready" else "legacy_unverified"
                    ),
                    "segments": [{"id": item.id, "page": item.page, "text": item.text,
                                  "start": item.start_offset, "end": item.end_offset,
                                  "status": item.status, "error": item.error} for item in segments]}

    @staticmethod
    def _verified_body(session, version) -> tuple[list, str]:
        """全文和模型读取共用校验，不把数据库中的意外改写当成原来的固定版本。"""
        segments = list(session.scalars(select(SourceSegment).where(
            SourceSegment.source_version_id == version.id,
        ).order_by(SourceSegment.page.asc().nulls_first(), SourceSegment.position)))
        text = "".join(item.text for item in segments)
        if version.content_hash_kind == "body-v1" and not version.body_hash:
            raise ValueError("source body integrity metadata is missing")
        if version.body_hash and hashlib.sha256(text.encode("utf-8")).hexdigest() != version.body_hash:
            raise ValueError("source body integrity check failed")
        return segments, text

    def cached_web(self, origin: str) -> dict | None:
        """复用最近一小时的网页版本；显式 refresh 可绕过，旧版本不删除。"""
        with self.sessions() as session:
            source = session.scalar(
                select(Source).where(Source.kind == "web", Source.origin == origin, Source.deleted_at.is_(None))
            )
            if source is None:
                return None
            version = session.scalar(
                select(SourceVersion)
                .where(
                    SourceVersion.source_id == source.id,
                    SourceVersion.status == "ready",
                    SourceVersion.content_hash_kind == "body-v1",
                    SourceVersion.created_at >= utcnow() - timedelta(hours=1),
                )
                .order_by(SourceVersion.number.desc())
                .limit(1)
            )
            return self._version_view(session, source, version) if version else None

    def read_segment(self, segment_id: str, *, root_id: str) -> dict:
        """实际读取成功才登记证据；失败页/空片段不能作为模型已读证据。"""
        with self.sessions.begin() as session:
            lock_control(session)
            segment = session.get(SourceSegment, segment_id)
            if segment is None or segment.status != "ready" or not segment.text.strip():
                raise ValueError("source segment is unavailable or failed to parse")
            version = session.get(SourceVersion, segment.source_version_id)
            claim_source(session, version.source_id, root_id=root_id, allow_removed=True)
            self._verified_body(session, version)
            evidence = Evidence(
                root_id=root_id,
                source_segment_id=segment.id,
                start_offset=segment.start_offset,
                end_offset=segment.end_offset,
                quote=segment.text,
            )
            session.add(evidence)
            session.flush()
            evidence_id = evidence.id
        return self.get_evidence(evidence_id)

    def get_segment(self, segment_id: str) -> dict:
        """用户查看解析结果，不把浏览器 GET 伪装为 Agent 已读取证据。"""
        with self.sessions() as session:
            segment = session.get(SourceSegment, segment_id)
            if segment is None:
                raise KeyError("source segment not found")
            self._verified_body(session, session.get(SourceVersion, segment.source_version_id))
            return {
                "id": segment.id,
                "source_version_id": segment.source_version_id,
                "position": segment.position,
                "page": segment.page,
                "text": segment.text,
                "start": segment.start_offset,
                "end": segment.end_offset,
                "method": segment.method,
                "status": segment.status,
                "error": segment.error,
            }

    def evidence_page(self, root_id: str, *, query: str = "", current_source_ids=(),
                      used_ids=(), offset: int = 0, limit: int = 40,
                      deduplicate: bool = True) -> dict:
        """分页选择实际已读固定证据，不增加引用权限，不受旧200项注入上限限制。

        同版本同范围的重复读取合并；已使用的引用优先，再考虑当前材料、关键词
        与读取时间。不同版本/片段不合并，以免丢掉真实定位。检索只在本根任务范围。
        """
        if not 0 <= offset or not 1 <= limit <= 200:
            raise ValueError("证据目录分页参数无效")
        from knowledgeops_agent.vendor.deerflow.history_terms import terms as query_terms

        terms = query_terms(query[:256])[:8]
        used = case((Evidence.id.in_(tuple(used_ids)), 1), else_=0)
        current = case((Source.id.in_(tuple(current_source_ids)), 1), else_=0)
        title = func.coalesce(Source.title, KnowledgeVersion.title, "")
        relevant = sum((case((or_(title.icontains(term, autoescape=True),
                                  Evidence.quote.icontains(term, autoescape=True)), 1), else_=0)
                        for term in terms), start=literal(0))
        ranked = select(
            Evidence.id.label("id"),
            func.row_number().over(
                partition_by=(Evidence.source_segment_id, Evidence.knowledge_version_id,
                              Evidence.start_offset, Evidence.end_offset),
                order_by=(used.desc(), Evidence.created_at.desc(), Evidence.id.desc()),
            ).label("duplicate_position"),
        ).where(Evidence.root_id == root_id).subquery()
        selection = (select(Evidence.id)
                     .join(ranked, ranked.c.id == Evidence.id)
                     .outerjoin(SourceSegment, SourceSegment.id == Evidence.source_segment_id)
                     .outerjoin(SourceVersion, SourceVersion.id == SourceSegment.source_version_id)
                     .outerjoin(Source, Source.id == SourceVersion.source_id)
                     .outerjoin(KnowledgeVersion, KnowledgeVersion.id == Evidence.knowledge_version_id)
                     .where(ranked.c.duplicate_position == 1 if deduplicate else literal(True)))
        with self.sessions() as session:
            total = int(session.scalar(select(func.count()).select_from(selection.subquery())) or 0)
            ids = list(session.scalars(selection.order_by(used.desc(), current.desc(), relevant.desc(),
                                                           Evidence.created_at.desc(), Evidence.id.desc())
                                      .offset(offset).limit(limit)))
        catalog = []
        for evidence_id in ids:
            row = self.get_evidence(evidence_id)
            entry = {key: row[key] for key in ("id", "title", "citation")}
            entry["observed_range"] = {"start": row["start"], "end": row["end"]}
            if "segment_id" in row:
                entry["read_tool"] = "read_source"
                entry["read_args"] = {"segment_id": row["segment_id"]}
            else:
                entry["read_tool"] = "read_knowledge"
                entry["read_args"] = {
                    "document_id": row["document_id"],
                    "version_id": row["knowledge_version_id"],
                    "start": row["start"],
                }
            catalog.append(entry)
        return {"items": catalog, "total": total, "next_offset": offset + len(catalog)
                if offset + len(catalog) < total else None, "deduplicated": deduplicate}

    def evidence_catalog(self, root_id: str) -> list[dict]:
        """保留旧内部调用合同；新模型上下文用有排序和分页入口的 evidence_page。"""
        return self.evidence_page(root_id, limit=200, deduplicate=False)["items"]

    def get_evidence(self, evidence_id: str) -> dict:
        """历史引用直接解析固定版本，不追随文档的当前版本指针。"""
        with self.sessions() as session:
            evidence = session.get(Evidence, evidence_id)
            if evidence is None:
                raise KeyError("evidence not found")
            result = {
                "id": evidence.id,
                "evidence_id": evidence.id,
                "root_id": evidence.root_id,
                "text": evidence.quote,
                "start": evidence.start_offset,
                "end": evidence.end_offset,
            }
            if evidence.source_segment_id:
                segment = session.get(SourceSegment, evidence.source_segment_id)
                version = session.get(SourceVersion, segment.source_version_id)
                source = session.get(Source, version.source_id)
                result.update(
                    {
                        "title": source.title,
                        "source_id": source.id,
                        "source_version_id": version.id,
                        "segment_id": segment.id,
                        "page": segment.page,
                        "origin": source.origin,
                    }
                )
            else:
                version = session.get(KnowledgeVersion, evidence.knowledge_version_id)
                result.update(
                    {
                        "title": version.title,
                        "document_id": version.document_id,
                        "knowledge_version_id": version.id,
                        "version": version.number,
                    }
                )
            label = result["title"].replace("[", "（").replace("]", "）")
            result["citation"] = f"[{label}](/api/evidence/{evidence.id})"
            return result

    def original(self, version_id: str) -> tuple[bytes, str]:
        """供下载/来源查看读取内容寻址对象，并验证磁盘文件未被篡改。"""
        with self.sessions() as session:
            version = session.get(SourceVersion, version_id)
            if version is None:
                raise KeyError("source version not found")
            if not version.storage_key:
                raise ValueError("source has no retained original; use saved body")
            path = (self.storage_root / version.storage_key).resolve()
            if not path.is_relative_to(self.storage_root):
                raise PermissionError("source path outside project storage")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != (version.original_hash or version.content_hash):
                raise ValueError("source integrity check failed")
            return data, version.mime_type
