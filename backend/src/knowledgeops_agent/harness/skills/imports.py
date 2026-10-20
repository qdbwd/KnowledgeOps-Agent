"""技能包的纯数据导入：解析、预览和规范化，不执行模块或安装依赖。

包边界与先检查后落盘借鉴 DeerFlow cb90f5dc 的 installer；诊断与渐进读取
借鉴 Pi d2e7d8b7。本模块是项目适配，保持已有 Deep Agents 运行加载接口。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

import yaml

from knowledgeops_agent.harness.skills.packages import (
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_FILES,
    MAX_FILE_BYTES,
    UNSUPPORTED_BEHAVIOR_FIELDS,
    SkillPackageError,
)

NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
# 声明中的授权、钩子或动态工具不属于本轮基础 Skill 格式，不能忽略后启用。


def package_digest(files: dict[str, bytes]) -> str:
    """对原始文件清单计算身份；恢复损坏元数据时也无需先解析正文。"""
    return hashlib.sha256(json.dumps(
        {path: hashlib.sha256(raw).hexdigest() for path, raw in files.items()}, sort_keys=True,
    ).encode()).hexdigest()


@dataclass
class PackageInspection:
    name: str
    description: str
    files: dict[str, bytes]
    missing: list[str]
    issues: list[str]
    compatibility: str
    digest: str

    @property
    def compatible(self) -> bool:
        return not self.issues

    def preview(self) -> dict:
        """页面只收到展示数据；二进制内容不会被强行转为可执行文本。"""
        resources = []
        for path, data in self.files.items():
            try:
                data.decode("utf-8")
                binary = False
            except UnicodeDecodeError:
                binary = True
            resources.append({"path": path, "size": len(data), "binary": binary})
        return {
            "name": self.name, "description": self.description,
            "missing_metadata": self.missing, "compatibility_issues": self.issues,
            "compatible": self.compatible, "compatibility": self.compatibility,
            "runtime_status": "not_tested", "content_hash": self.digest,
            "content": self.files["SKILL.md"].decode("utf-8"),
            "resources": resources, "total_bytes": sum(len(data) for data in self.files.values()),
        }


def safe_relative_path(value: str) -> str:
    """同时检查 Linux 提取与 Windows 项目目录的含义，不修补可疑路径。"""
    if not isinstance(value, str) or len(value) > 512 or any(c in value for c in "\\:\x00"):
        raise SkillPackageError("技能文件路径无效")
    parts = value.split("/")
    if any(
        not part or part in {".", ".."} or part.startswith(".") or part.endswith((".", " "))
        or part.split(".")[0].lower() in RESERVED or len(part) > 200
        or any(ord(c) < 32 or c in '<>"|?*' for c in part)
        for part in parts
    ):
        raise SkillPackageError("技能包包含隐藏、越界或不可移植的文件路径")
    if any(part in {"__pycache__", "node_modules", "venv"} for part in parts):
        raise SkillPackageError("请移除技能包中的依赖环境和缓存，仅上传源文件及必要资源")
    return "/".join(parts)


def _checked_files(files: list[tuple[str, bytes]]) -> dict[str, bytes]:
    if not files or len(files) > MAX_BUNDLE_FILES:
        raise SkillPackageError("技能包为空或文件数量超过限制")
    result, seen, total = {}, set(), 0
    for path, data in files:
        path = safe_relative_path(path)
        if path.casefold() in seen:
            raise SkillPackageError("技能包存在重复文件名（包括仅大小写不同）")
        if not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES:
            raise SkillPackageError("技能文件大小超过限制")
        total += len(data)
        if total > MAX_BUNDLE_BYTES:
            raise SkillPackageError("技能包大小超过限制")
        seen.add(path.casefold())
        result[path] = data
    for path in result:
        if any(str(parent).casefold() in seen for parent in PurePosixPath(path).parents if str(parent) != "."):
            raise SkillPackageError("技能包存在文件与目录重名")
    return result


def unpack_zip(data: bytes) -> list[tuple[str, bytes]]:
    """按声明大小及实际读取量双重限流，拒绝链接/特殊文件，不直接 extractall。"""
    if len(data) > MAX_BUNDLE_BYTES + MAX_FILE_BYTES:
        raise SkillPackageError("ZIP 大小超过限制")
    result = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            if len(bundle.infolist()) > MAX_BUNDLE_FILES * 2:
                raise SkillPackageError("ZIP 条目数量超过限制")
            declared = 0
            for info in bundle.infolist():
                path = safe_relative_path(info.filename.rstrip("/"))
                mode = stat.S_IFMT(info.external_attr >> 16)
                if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise SkillPackageError("ZIP 包含链接或非普通文件")
                if info.is_dir():
                    continue
                if mode == stat.S_IFDIR or info.flag_bits & 1:
                    raise SkillPackageError("ZIP 包含加密文件或异常目录")
                declared += info.file_size
                if info.file_size > MAX_FILE_BYTES or declared > MAX_BUNDLE_BYTES:
                    raise SkillPackageError("ZIP 解压后大小超过限制")
                with bundle.open(info) as stream:
                    raw = stream.read(MAX_FILE_BYTES + 1)
                if len(raw) != info.file_size:
                    raise SkillPackageError("ZIP 文件大小不一致")
                result.append((path, raw))
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as exc:
        raise SkillPackageError("ZIP 无效或使用了不支持的压缩方式") from exc
    return list(_checked_files(result).items())


def inspect_package(
    files: list[tuple[str, bytes]], *, name: str | None = None, description: str | None = None,
) -> PackageInspection:
    """接受单文档或一层包目录；嵌套 SKILL.md 保留为资源，不注册第二个技能。"""
    values = _checked_files(files)
    entries = [PurePosixPath(path) for path in values if PurePosixPath(path).name.casefold() == "skill.md"]
    if not entries:
        raise SkillPackageError("技能包需要 SKILL.md")
    depth = min(len(path.parts) for path in entries)
    roots = [path for path in entries if len(path.parts) == depth]
    if len(roots) != 1:
        raise SkillPackageError("一次只能导入一个技能包")
    entry = roots[0]
    prefix = entry.parent
    try:
        values = {str(PurePosixPath(path).relative_to(prefix)): data for path, data in values.items()}
    except ValueError as exc:
        raise SkillPackageError("所有文件必须位于同一个技能包目录") from exc
    values["SKILL.md"] = values.pop(entry.name)
    try:
        text = values["SKILL.md"].decode("utf-8-sig")
    except UnicodeError as exc:
        raise SkillPackageError("SKILL.md 必须使用 UTF-8 文本编码") from exc
    match = FRONTMATTER.match(text)
    if text.startswith("---\n") or text.startswith("---\r\n"):
        if match is None:
            raise SkillPackageError("YAML 元数据缺少结束分隔符")
    try:
        metadata = yaml.safe_load(match[1]) if match else {}
    except yaml.YAMLError as exc:
        raise SkillPackageError("YAML 元数据格式错误，请修正后重新上传") from exc
    if not isinstance(metadata, dict) or not isinstance(metadata.get("metadata", {}), dict):
        raise SkillPackageError("YAML 元数据必须为字段对象")
    resolved_name = name if name is not None else metadata.get("name", "")
    resolved_description = description if description is not None else metadata.get("description", "")
    if not isinstance(resolved_name, str) or not isinstance(resolved_description, str):
        raise SkillPackageError("名称和用途必须为文本")
    resolved_name, resolved_description = resolved_name.strip(), resolved_description.strip()
    if resolved_name and (len(resolved_name) > 64 or not NAME.fullmatch(resolved_name)):
        raise SkillPackageError("技能名称使用不超过64位的小写英文、数字和单个连字符")
    if len(resolved_description) > 1024:
        raise SkillPackageError("技能用途不超过1024字符")
    missing = [field for field, value in [("name", resolved_name), ("description", resolved_description)] if not value]
    body = text[match.end():] if match else text
    if not body.strip():
        raise SkillPackageError("SKILL.md 需要正文说明")
    # 只有用户补填或更正元数据时才重写头部，正文及配套文件逐字节保留。
    if not missing and (not match or metadata.get("name") != resolved_name or metadata.get("description") != resolved_description):
        metadata = {**metadata, "name": resolved_name, "description": resolved_description}
        text = "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n" + body
    values["SKILL.md"] = text.encode("utf-8")
    values = _checked_files(list(values.items()))
    issues = [f"当前不支持 {field} 的运行语义，请先适配此声明后启用。" for field in sorted(UNSUPPORTED_BEHAVIOR_FIELDS & metadata.keys())]
    compatibility = metadata.get("compatibility", "")
    digest = package_digest(values)
    return PackageInspection(
        name=resolved_name, description=resolved_description, files=values, missing=missing,
        issues=issues, compatibility=compatibility if isinstance(compatibility, str) else "", digest=digest,
    )
