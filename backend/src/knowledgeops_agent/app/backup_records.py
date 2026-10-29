"""备份与恢复记录的只读投影。

网页只读展示本机命令产生的归档、校验与恢复结果，不控制 Docker、不下载 dump、
不覆盖正式库。只读取固定目录下的元数据文件；目录存在不等于成功，失败或中断
的记录保留供诊断。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from knowledgeops_agent.core.config import PROJECT_ROOT

MAX_ITEMS = 50


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def default_root() -> Path | None:
    """显式路径变量优先，便于隔离验收指向夹具目录；默认只读项目 artifacts。"""
    override = os.environ.get("BACKUP_ARTIFACT_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(PROJECT_ROOT) / "artifacts" / "backups" if PROJECT_ROOT is not None else None


class BackupRecords:
    def __init__(self, root: Path | None = None):
        self.root = root or default_root()

    def list(self, limit: int = 20) -> dict:
        if self.root is None or not self.root.is_dir():
            return {"available": False, "detail": "备份目录不在当前部署中，记录需在本机查看",
                    "items": [], "truncated": False}
        directories = sorted((item for item in self.root.iterdir() if item.is_dir()),
                             key=lambda item: item.name, reverse=True)
        items = [self._item(directory) for directory in directories[: max(1, min(limit, MAX_ITEMS))]]
        return {"available": True, "items": items, "truncated": len(directories) > len(items)}

    @staticmethod
    def _operations(directory: Path) -> list[dict]:
        folder = directory / "operations"
        if not folder.is_dir():
            return []
        items = []
        for path in sorted(folder.glob("*.json")):
            operation = _read_json(path)
            if not isinstance(operation, dict):
                continue
            items.append({
                "id": str(operation.get("id") or path.stem),
                "kind": str(operation.get("kind") or "unknown"),
                "state": str(operation.get("state") or operation.get("status") or "unknown"),
                "phase": str(operation.get("phase") or ""),
                "started_at": operation.get("started_at"),
                "updated_at": operation.get("updated_at"),
                "report": str(operation.get("report") or ""),
                "database": str(operation.get("database") or ""),
            })
        return items[-6:]

    def _item(self, directory: Path) -> dict:
        manifest = _read_json(directory / "manifest.json")
        verification = _read_json(directory / "restore-verification.json")
        checks = verification.get("checks") if isinstance(verification, dict) else None
        failed = sorted(name for name, value in (checks or {}).items() if not value)
        passed = isinstance(checks, dict) and bool(checks) and not failed
        if manifest and passed:
            status, detail = "verified", "已发布清单并通过隔离恢复校验"
        elif manifest:
            status, detail = "created", "已发布清单，尚无通过的隔离恢复校验"
        elif not directory.is_dir():
            status, detail = "incomplete", "记录不可读"
        else:
            status, detail = "incomplete", "没有成功清单：创建失败或中断，目录保留供诊断"
        return {
            "id": directory.name,
            "created_at": manifest.get("created_at") if isinstance(manifest, dict) else None,
            "format": str(manifest.get("format")) if isinstance(manifest, dict) else "",
            "status": status,
            "detail": detail,
            "operations": self._operations(directory),
            "restore": {
                "checked": isinstance(checks, dict),
                "passed": passed,
                "failed_checks": failed,
                "database": str(verification.get("database") or "") if isinstance(verification, dict) else "",
            },
        }
