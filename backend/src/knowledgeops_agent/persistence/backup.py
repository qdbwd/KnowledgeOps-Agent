"""备份集合与恢复证据的校验；成功恢复必须绑定同一份归档，不能只相信状态字段。"""

import hashlib
import json
from pathlib import Path


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verified_manifest(folder: Path) -> dict:
    folder = folder.resolve()
    manifest_path = folder / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    archive = {1: "sources.tar.gz", 2: "workspace.tar.gz"}.get(manifest.get("format"))
    if archive is None or set(manifest.get("files", {})) != {"business.dump", "accounting.dump", archive}:
        raise ValueError("unsupported or incomplete backup manifest")
    for name, digest in manifest["files"].items():
        path = folder / name
        if path.is_symlink() or not path.is_file() or file_hash(path) != digest:
            raise ValueError("backup archive integrity check failed")
    return {**manifest, "manifest_sha256": file_hash(manifest_path)}


def verified_restore(folder: Path, *, source_database: str) -> dict:
    """用于显式清理的前提：同库完整备份已恢复且本次归档字节没有改变。"""
    manifest = verified_manifest(folder)
    report = json.loads((folder / "restore-verification.json").read_text(encoding="utf-8"))
    required = {"all_original_hashes", "all_saved_bodies_readable", "all_citations_resolve",
                "all_delivered_files_readable", "all_learning_profile_revisions_readable", "index_rebuild_equivalent"}
    checks = report.get("checks", {})
    if (manifest.get("source_database") != source_database
        or report.get("source_database") != source_database
        or report.get("backup_manifest_sha256") != manifest["manifest_sha256"]
        or not required.issubset(checks)
        or not checks or not all(value is True for value in checks.values())):
        raise ValueError("matching backup restore verification is required")
    return report
