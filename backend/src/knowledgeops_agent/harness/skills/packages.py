"""部署技能包的发现与快照；包内脚本是资源，不在发现阶段导入或执行。

包边界参考 DeerFlow 3343522 的 LocalSkillStorage 及对应回归测试。继续使用
Deep Agents 做模型侧渐进加载；这里仅适配本项目的固定目录、版本和启用配置。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath

import yaml

MAX_FILE_BYTES = 1024 * 1024
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_FILES = 2048
IGNORED_DIRECTORIES = {"__pycache__", "node_modules"}


class SkillPackageError(ValueError):
    """部署者可以定位到具体技能或资源的配置错误。"""


def is_skill_entry(row: dict) -> bool:
    """兼容旧快照；嵌套 SKILL.md 是参考/测试材料，不是新的技能身份。"""
    parts = PurePosixPath(row["resource"]).parts
    return len(parts) == 4 and parts[1] == "skills" and parts[-1] == "SKILL.md"


def disabled_skills(root: Path) -> dict[str, str]:
    """保留暂不兼容的原包，用明确原因控制发现；没有配置的新增包默认启用。"""
    path = root / "availability.json"
    if not path.exists():
        return {}
    if path.is_symlink():
        raise SkillPackageError("技能启用配置不能使用符号链接")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as error:
        raise SkillPackageError("技能启用配置无效：availability.json") from error
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or not isinstance(value.get("disabled"), dict)
        or any(
            not isinstance(name, str) or not isinstance(reason, str) or not reason.strip()
            for name, reason in value["disabled"].items()
        )
    ):
        raise SkillPackageError("技能启用配置无效：availability.json")
    return value["disabled"]


def _bytes(path: Path, root: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise SkillPackageError(f"技能资源必须在声明的技能目录内：{path.name}")
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise SkillPackageError(f"技能文件超过 1 MiB：{path.relative_to(root)}")
    return raw


def _entry(root: Path, directory: Path, disabled: dict) -> dict:
    if directory.is_symlink():
        raise SkillPackageError("技能目录不能使用符号链接")
    raw = _bytes(directory / "SKILL.md", root)
    resource = f"/skills/{directory.name}/SKILL.md"
    try:
        content = raw.decode("utf-8")
        parts = content.split("---", 2)
        meta = yaml.safe_load(parts[1]) if len(parts) == 3 and not parts[0].strip() else {}
    except (UnicodeError, yaml.YAMLError) as error:
        raise SkillPackageError(f"技能声明无效：{resource}") from error
    if (
        not isinstance(meta, dict)
        or not isinstance(meta.get("name"), str)
        or not meta["name"].strip()
        or not isinstance(meta.get("description"), str)
        or not meta["description"].strip()
        or not isinstance(meta.get("metadata", {}), dict)
    ):
        raise SkillPackageError(f"技能声明无效：{resource}")
    if meta["name"] != directory.name:
        raise SkillPackageError(f"技能名称必须唯一且与目录名一致：{resource}")
    digest = hashlib.sha256(raw).hexdigest()
    reason = disabled.get(directory.name)
    status = "disabled" if reason else "available"
    if "allowed-tools" in meta:
        status = "incompatible"
        reason = "当前尚未实现 Skill allowed-tools 强制约束；此包暂不可用，不能忽略声明执行。"
    return {
        "name": meta["name"], "description": meta["description"],
        "version": str(meta.get("metadata", {}).get("version") or digest[:12]),
        "resource": resource, "enabled": reason is None,
        "unavailable_reason": reason, "availability_status": status,
    }


def _package_resources(root: Path, entry: dict) -> tuple[list[dict], int]:
    """整个包验证后才提交到快照；失败时不能残留已读取的脚本或入口。"""
    result: list[dict] = []
    total = 0
    directory = root / entry["name"]
    def fail_walk(error):
        raise error

    for current, directories, filenames in os.walk(directory, followlinks=False, onerror=fail_walk):
        directories[:] = sorted(
            name for name in directories
            if not name.startswith(".") and name not in IGNORED_DIRECTORIES
        )
        if any((Path(current) / name).is_symlink() for name in directories):
            raise SkillPackageError(f"技能资源目录不能使用符号链接：{entry['name']}")
        for name in sorted(filenames):
            if name.startswith(".") or name.endswith((".pyc", ".pyo")):
                continue
            path = Path(current) / name
            raw = _bytes(path, root)
            total += len(raw)
            if total > MAX_BUNDLE_BYTES or len(result) >= MAX_BUNDLE_FILES:
                raise SkillPackageError("技能包超过 8 MiB 或 2048 个文件，请缩小包范围")
            try:
                content, encoding = raw.decode("utf-8"), "utf-8"
            except UnicodeError:
                content, encoding = base64.b64encode(raw).decode("ascii"), "base64"
            resource = "/skills/" + path.relative_to(root).as_posix()
            result.append({
                "resource": resource,
                "name": entry["name"] if resource == entry["resource"] else path.stem,
                "version": entry["version"] if resource == entry["resource"] else "resource-v2",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "content": content, "encoding": encoding,
            })
    return result, total


def _invalid_entry(name: str, reason: str, *, status="invalid") -> dict:
    return {
        "name": name, "description": "此技能配置需要修复后才能用于新任务。",
        "resource": f"/skills/{name}/SKILL.md", "version": "unavailable",
        "enabled": False, "unavailable_reason": reason, "availability_status": status,
    }


def scan_packages(root: Path, *, excluded: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """可选包逐个隔离；全局策略坏时关闭所有技能，不放行也不阻断普通对话。"""
    try:
        if not root.is_dir():
            raise SkillPackageError("项目技能目录缺失")
        disabled = disabled_skills(root)
        directories = sorted(root.iterdir())
    except (SkillPackageError, OSError) as error:
        return [_invalid_entry("__configuration__", str(error), status="config_error")], []
    catalog: list[dict] = []
    result: list[dict] = []
    total = 0
    for directory in directories:
        if directory.name.startswith("."):
            continue
        if not directory.is_symlink() and (
            not directory.is_dir() or not (directory / "SKILL.md").is_file()
        ):
            continue
        try:
            entry = _entry(root, directory, disabled)
            if entry["enabled"] and entry["name"] not in (excluded or set()):
                rows, size = _package_resources(root, entry)
                if total + size > MAX_BUNDLE_BYTES or len(result) + len(rows) > MAX_BUNDLE_FILES:
                    raise SkillPackageError("启用技能快照超过 8 MiB 或 2048 个文件，请缩小启用范围")
                result.extend(rows)
                total += size
        except (SkillPackageError, OSError, RuntimeError) as error:
            entry = _invalid_entry(directory.name, str(error))
        catalog.append(entry)
    return catalog, result


def package_catalog(root: Path) -> list[dict]:
    """管理目录包含无效包的原因；资料目录和嵌套示例不注册为技能。"""
    return scan_packages(root)[0]


def load_package_resources(root: Path, *, excluded: set[str] | None = None) -> list[dict]:
    """保存已启用包的字节。脚本/模板不占初始提示词，只在需要时读取/执行。"""
    return scan_packages(root, excluded=excluded)[1]


def snapshot_files(rows: list[dict]) -> dict[str, bytes]:
    """解码固定字节；旧纯文本快照无 encoding，保持原语义。"""
    return {
        row["resource"]: (
            base64.b64decode(row["content"], validate=True)
            if row.get("encoding") == "base64" else row["content"].encode("utf-8")
        )
        for row in rows
    }
