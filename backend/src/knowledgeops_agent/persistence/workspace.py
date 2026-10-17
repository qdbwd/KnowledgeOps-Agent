"""文件迁移与当前知识视图；只处理项目管理区，不扫描宿主其他目录。"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

from sqlalchemy import select, text

from knowledgeops_agent.knowledge.export import export_markdown
from knowledgeops_agent.persistence.models import KnowledgeVersion, RunArtifact, SourceVersion


def migrate_content(sessions, *, legacy_source_root: Path | None = None) -> dict:
    """可重入回填：先校验文件，再清空旧正文列，ID/引用/搜索排名保持不变。

    旧原件目录只读；不删除卷、不清空账本。先完成数据库迁移 0018，再调用。
    回滚只可能留下尚未引用的文件，下次按同一 ID/哈希继续，不覆盖已有内容。
    """
    counts = {"knowledge_versions": 0, "run_artifacts": 0, "source_originals": 0}
    with sessions.begin() as session:
        store = session.info["file_store"]
        for row in session.scalars(select(KnowledgeVersion).with_for_update()):
            if row.body_storage_key:
                store.read(row.body_storage_key, row.body_sha256)
                continue
            key = f"knowledge/versions/{row.id}.md"
            row.body_sha256 = store.put(key, row._body)
            row.body_storage_key, row._body = key, ""
            counts["knowledge_versions"] += 1
        for row in session.scalars(select(RunArtifact).with_for_update()):
            if row.content_storage_key:
                store.read(row.content_storage_key, row.content_hash)
                continue
            key = f"artifacts/{row.run_id}/{row.content_hash}.txt"
            if store.put(key, row._content) != row.content_hash:
                raise ValueError("legacy artifact hash mismatch")
            row.content_storage_key, row._content = key, ""
            counts["run_artifacts"] += 1
        for row in session.scalars(select(SourceVersion)):
            if not row.storage_key:
                continue
            destination = store.path("uploads/originals/" + row.storage_key)
            if destination.is_file():
                data = destination.read_bytes()
            elif legacy_source_root:
                origin = (legacy_source_root.resolve() / row.storage_key).resolve()
                if not origin.is_relative_to(legacy_source_root.resolve()):
                    raise ValueError("legacy source path escapes storage root")
                data = origin.read_bytes()
                if hashlib.sha256(data).hexdigest() != row.content_hash:
                    raise ValueError("legacy source hash mismatch")
                store.replace("uploads/originals/" + row.storage_key, data)
            else:
                raise FileNotFoundError("missing original; provide legacy_source_root")
            if hashlib.sha256(data).hexdigest() != row.content_hash:
                raise ValueError("workspace source hash mismatch")
            counts["source_originals"] += 1
    return counts


def sync_knowledge_projection(sessions) -> dict:
    """复用现有离线导出，生成 current 视图；失败不撤销已提交的正式版本。

    PG 是版本指针的事实来源，versions 文件是正文事实来源。current 可重建，
    manifest 最后原子替换作为完成标记；跨文件不宣称事务原子性。通过 PG 锁
    串行发布后的视图刷新，防止较旧导出覆盖较新版本。
    """
    with sessions.begin() as session:
        store = session.info.get("file_store")
        if store is None:
            return {"status": "disabled"}
        session.execute(text("SELECT pg_advisory_xact_lock(570740181)"))
        archive_bytes = export_markdown(sessions)
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            base = "knowledge/current/"
            previous = store.path(base + "manifest.json")
            old_paths = set()
            if previous.is_file():
                old_paths = set(json.loads(previous.read_text()).get("managed_files", []))
            names = set(archive.namelist()) - {"manifest.json"}
            for name in sorted(names):
                store.replace(base + name, archive.read(name))
            index = [
                "# 当前知识库",
                "",
                "此目录由发布记录生成；修改请通过应用发布，勿直接编辑此视图。",
                "",
            ]
            for item in manifest["documents"]:
                if not item["reference_only"]:
                    title = item["title"].replace("[", "（").replace("]", "）").replace("\n", " ")
                    index.append(f"- [{title}]({item['path']}) · 版本 {item['version']}")
            if not manifest["documents"]:
                index.append("尚未发布知识正文。可先将材料放入 work，再让 Agent 整理并发布。")
            store.replace(base + "README.md", "\n".join(index).encode())
            names.add("README.md")
            # 只清理由自己上次列入清单、且本次不再输出的视图文件；不动正文版本。
            for name in old_paths - names:
                target = store.path(base + name)
                if not target.is_relative_to(store.path("knowledge/current")):
                    raise ValueError("invalid projection manifest path")
                target.unlink(missing_ok=True)
            manifest["managed_files"] = sorted(names)
            store.replace(
                base + "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode()
            )
        return {
            "status": "ready",
            "path": "/knowledge/current/",
            "documents": len(manifest["documents"]),
        }
