"""原始材料、不可变解析版本与真实读取证据。

文件名从内容哈希生成，显示标题不会拼进路径。证据由读取操作生成而非模型
自报，后续正文只引用可解析的证据 ID；网页/PDF 适配器复用此存储边界。
"""

from __future__ import annotations

import hashlib
import os
from datetime import timedelta
from pathlib import Path

from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert

from knowledgeops_agent.persistence.models import (
    Evidence,
    KnowledgeVersion,
    Source,
    SourceSegment,
    SourceVersion,
    new_id,
    utcnow,
)
from knowledgeops_agent.persistence.temporary import ManagedTemporary


class SourceService:
    def __init__(self, sessions, storage_root: Path):
        self.sessions = sessions
        self.storage_root = storage_root.resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)

    def _store_original(self, data: bytes) -> tuple[str, str]:
        """先原子保存不可变原件；DB 回滚最多留下可回收孤立对象，不留下假来源。"""
        digest = hashlib.sha256(data).hexdigest()
        key = f"{digest[:2]}/{digest}"
        target = self.storage_root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with ManagedTemporary(self.storage_root, kind="upload") as temporary:
                temporary.stream.write(data)
                temporary.stream.flush()
                os.fsync(temporary.stream.fileno())
                os.replace(temporary.path, target)
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
    ) -> dict:
        """导入文本并按 2400 字符建立可定位片段；相同来源/内容不重复版本。

        origin 存在时识别同一外部来源，否则以内容哈希识别独立粘贴材料。
        原始字节与提取正文可分开，网页快照不会被错误替换为搜索摘要。
        """
        if not title.strip() or len(title) > 500 or not content.strip():
            raise ValueError("nonempty title (<=500 characters) and content are required")
        digest, key = self._store_original(
            raw_bytes if raw_bytes is not None else content.encode("utf-8")
        )
        identity = f"{kind}:{origin or digest}"
        with self.sessions.begin() as session:
            session.execute(
                insert(Source)
                .values(id=new_id(), identity_key=identity, title=title, kind=kind, origin=origin)
                .on_conflict_do_nothing(index_elements=["identity_key"])
            )
            source = session.scalar(
                select(Source).where(Source.identity_key == identity).with_for_update()
            )
            version = session.scalar(
                select(SourceVersion).where(
                    SourceVersion.source_id == source.id, SourceVersion.content_hash == digest
                )
            )
            if version is None:
                number = (
                    session.scalar(
                        select(func.count())
                        .select_from(SourceVersion)
                        .where(SourceVersion.source_id == source.id)
                    )
                    + 1
                )
                version = SourceVersion(
                    source_id=source.id,
                    number=number,
                    content_hash=digest,
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

    def register_pdf(self, title: str, data: bytes) -> dict:
        """上传阶段只保存原件与待解析版本；耗时的文本/OCR 工作留给 worker。"""
        if not data.startswith(b"%PDF-") or not title.strip() or len(data) > 10 * 1024 * 1024:
            raise ValueError("PDF must have a valid header and be at most 10 MiB")
        digest, key = self._store_original(data)
        identity = f"pdf:{digest}"
        with self.sessions.begin() as session:
            session.execute(
                insert(Source)
                .values(id=new_id(), identity_key=identity, title=title[:500], kind="pdf")
                .on_conflict_do_nothing(index_elements=["identity_key"])
            )
            source = session.scalar(
                select(Source).where(Source.identity_key == identity).with_for_update()
            )
            version = session.scalar(
                select(SourceVersion).where(SourceVersion.source_id == source.id)
            )
            if version is None:
                version = SourceVersion(
                    source_id=source.id,
                    number=1,
                    content_hash=digest,
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
            "deleted": source.deleted_at is not None,
            "version_id": version.id,
            "version": version.number,
            "status": version.status,
            "error": version.error,
            "content_hash": version.content_hash,
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

    def get_source(self, source_id: str, *, version_id: str | None = None) -> dict:
        with self.sessions() as session:
            source = session.get(Source, source_id)
            if source is None or (source.deleted_at is not None and version_id is None):
                raise KeyError("source not found")
            query = select(SourceVersion).where(SourceVersion.source_id == source_id)
            if version_id:
                query = query.where(SourceVersion.id == version_id)
            version = session.scalar(query.order_by(SourceVersion.number.desc()).limit(1))
            if version is None:
                raise KeyError("source version not found")
            return self._version_view(session, source, version)

    def list_sources(self) -> list[dict]:
        with self.sessions() as session:
            return [
                {
                    "id": source.id,
                    "title": source.title,
                    "kind": source.kind,
                    "origin": source.origin,
                }
                for source in session.scalars(select(Source).where(Source.deleted_at.is_(None)).order_by(Source.created_at.desc()))
            ]

    def remove_source(self, source_id: str) -> dict:
        """移出材料选择目录，不级联破坏聊天/知识引用；重新导入获得新身份。

        这是用户资产的逻辑删除，不借此擦除原件或修改不可变版本。重试幂等，
        改写去重键仅释放重新导入的位置，旧版本和证据 ID 全部保持不变。
        """
        with self.sessions.begin() as session:
            source = session.scalar(select(Source).where(Source.id == source_id).with_for_update())
            if source is None:
                raise KeyError("source not found")
            if source.deleted_at is None:
                source.deleted_at = utcnow()
                source.identity_key = f"deleted:{source.id}:{source.identity_key}"
            return {"id": source.id, "deleted": True, "references_preserved": True}

    def full_content(self, version_id: str) -> dict:
        """浏览器按固定版本读取全部解析正文，不新增 Agent 阅读证据。

        普通文本按偏移直接拼回原文；另返回页面数组供PDF阅读器区分失败页。
        原网页 HTML 继续只供下载，不能在同源页面执行其中脚本。
        """
        with self.sessions() as session:
            version = session.get(SourceVersion, version_id)
            if version is None:
                raise KeyError("source version not found")
            segments = list(session.scalars(select(SourceSegment).where(
                SourceSegment.source_version_id == version_id,
            ).order_by(SourceSegment.page.asc().nulls_first(), SourceSegment.position)))
            return {"version_id": version_id, "text": "".join(item.text for item in segments),
                    "segments": [{"id": item.id, "page": item.page, "text": item.text,
                                  "start": item.start_offset, "end": item.end_offset,
                                  "status": item.status, "error": item.error} for item in segments]}

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
                    SourceVersion.created_at >= utcnow() - timedelta(hours=1),
                )
                .order_by(SourceVersion.number.desc())
                .limit(1)
            )
            return self._version_view(session, source, version) if version else None

    def read_segment(self, segment_id: str, *, root_id: str) -> dict:
        """实际读取成功才登记证据；失败页/空片段不能作为模型已读证据。"""
        with self.sessions.begin() as session:
            segment = session.get(SourceSegment, segment_id)
            if segment is None or segment.status != "ready" or not segment.text.strip():
                raise ValueError("source segment is unavailable or failed to parse")
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
            path = (self.storage_root / version.storage_key).resolve()
            if not path.is_relative_to(self.storage_root):
                raise PermissionError("source path outside project storage")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != version.content_hash:
                raise ValueError("source integrity check failed")
            return data, version.mime_type
