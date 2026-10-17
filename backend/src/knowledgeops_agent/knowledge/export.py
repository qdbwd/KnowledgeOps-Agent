"""在一致数据库快照中导出文章与证据闭包，不把离线链接留在应用路由上。"""

from __future__ import annotations

import io
import json
import re
import zipfile
from typing import Any

from sqlalchemy import select, text

from knowledgeops_agent.persistence.models import (
    Evidence,
    KnowledgeDocument,
    KnowledgeRelation,
    KnowledgeVersion,
    Source,
    SourceSegment,
    SourceVersion,
    utcnow,
)

EVIDENCE_LINK = re.compile(r"/api/evidence/([a-fA-F0-9-]{36})")
KNOWLEDGE_LINK = re.compile(r"/knowledge/([a-fA-F0-9-]{36})(?:\?version=([a-fA-F0-9-]{36}))?")


def export_markdown(sessions, *, include_archived: bool = False) -> bytes:
    """默认导出未归档正文；被它们引用的归档/历史文章仅作为关联资料附带。

    一张表记录已安排的版本/证据，支持交叉链接与循环引用且不会无限递归。
    所有文件位于包的一层子目录，故相对链接统一使用 ../<目标路径>。
    缺失引用显式失败，不生成看似成功但无法打开的半份导出。
    """
    stream = io.BytesIO()
    manifest: dict[str, Any] = {
        "format": "knowledgeops-markdown-v1",
        "created_at": utcnow().isoformat(),
        "documents": [],
        "evidence": [],
    }
    with sessions.begin() as session, zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        current = {
            row.document_id: row
            for row in session.scalars(
                select(KnowledgeVersion)
                .join(
                    KnowledgeDocument, KnowledgeDocument.current_version_id == KnowledgeVersion.id
                )
                .order_by(KnowledgeDocument.id)
            )
        }
        documents: dict[str, tuple[KnowledgeVersion, str, bool]] = {}
        evidence_ids: set[str] = set()
        written_documents: set[str] = set()
        written_evidence: set[str] = set()
        for version in current.values():
            if include_archived or not version.archived:
                documents[version.id] = (version, f"documents/{version.document_id}.md", False)

        def document_path(document_id: str, version_id: str | None = None) -> str:
            version = (
                session.get(KnowledgeVersion, version_id)
                if version_id
                else current.get(document_id)
            )
            if version is None or version.document_id != document_id:
                raise ValueError("export contains an unresolved knowledge link")
            if version.id not in documents:
                documents[version.id] = (version, f"related/{document_id}-{version.id}.md", True)
            return documents[version.id][1]

        def rewrite(body: str) -> str:
            def evidence_link(match):
                evidence_ids.add(match[1])
                return f"../sources/{match[1]}.md"

            body = EVIDENCE_LINK.sub(evidence_link, body)
            return KNOWLEDGE_LINK.sub(lambda match: "../" + document_path(match[1], match[2]), body)

        while set(documents) - written_documents or evidence_ids - written_evidence:
            for version_id in sorted(set(documents) - written_documents):
                version, path, reference_only = documents[version_id]
                body = rewrite(version.body)
                relations = []
                for relation in session.scalars(
                    select(KnowledgeRelation).where(KnowledgeRelation.version_id == version.id)
                ):
                    target = document_path(relation.target_id)
                    label = relation.label.replace("[", "（").replace("]", "）")
                    relations.append(
                        {"target_id": relation.target_id, "label": relation.label, "path": target}
                    )
                    body += f"\n\n相关阅读：[{label}](../{target})"
                if reference_only:
                    body = "> 关联资料快照；归档状态与版本见 manifest.json。\n\n" + body
                archive.writestr(path, body)
                manifest["documents"].append(
                    {
                        "id": version.document_id,
                        "version_id": version.id,
                        "version": version.number,
                        "title": version.title,
                        "category": version.category,
                        "tags": version.tags,
                        "archived": version.archived,
                        "reference_only": reference_only,
                        "path": path,
                        "relations": relations,
                    }
                )
                written_documents.add(version_id)
            for evidence_id in sorted(evidence_ids - written_evidence):
                evidence = session.get(Evidence, evidence_id)
                if evidence is None:
                    raise ValueError("export contains an unresolved evidence link")
                metadata = {
                    "id": evidence.id,
                    "start": evidence.start_offset,
                    "end": evidence.end_offset,
                }
                if evidence.source_segment_id:
                    segment = session.get(SourceSegment, evidence.source_segment_id)
                    source_version = session.get(SourceVersion, segment.source_version_id)
                    source = session.get(Source, source_version.source_id)
                    metadata.update(
                        {
                            "source_id": source.id,
                            "source_version_id": source_version.id,
                            "title": source.title,
                            "origin": source.origin,
                            "page": segment.page,
                            "content_hash": source_version.content_hash,
                        }
                    )
                else:
                    knowledge_version = session.get(KnowledgeVersion, evidence.knowledge_version_id)
                    metadata.update(
                        {
                            "document_id": knowledge_version.document_id,
                            "knowledge_version_id": knowledge_version.id,
                            "title": knowledge_version.title,
                            "version": knowledge_version.number,
                        }
                    )
                description = "\n".join(
                    f"- {key}: {value}" for key, value in metadata.items() if value is not None
                )
                archive.writestr(
                    f"sources/{evidence_id}.md",
                    f"# 来源证据\n\n{description}\n\n## 原文\n\n{rewrite(evidence.quote)}\n",
                )
                manifest["evidence"].append(metadata)
                written_evidence.add(evidence_id)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return stream.getvalue()
