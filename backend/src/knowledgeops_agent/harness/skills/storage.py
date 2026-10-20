"""技能包暂存与目录替换日志；数据库决定提交与否，目录只保存可恢复字节。

沿用项目内容安装的先记归属、fsync后替换原则，适配可变包目录。所有调用由
管理服务持配置锁；日志不执行内容，也不把任意目录纳入清理范围。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from uuid import UUID

from knowledgeops_agent.harness.skills.imports import (
    NAME,
    PackageInspection,
    _checked_files,
    inspect_package,
)
from knowledgeops_agent.harness.skills.packages import (
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_FILES,
    SkillPackageError,
    _bytes,
)

FORMAT = "knowledgeops-skill-management-v1"


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class SkillStorage:
    def __init__(self, root: Path):
        self.root = root.absolute()

    def checked(self, relative: str) -> Path:
        parts = relative.split("/")
        if any(part in {"", ".", ".."} or "\\" in part for part in parts):
            raise SkillPackageError("技能管理路径无效")
        path = self.root
        if path.is_symlink():
            raise SkillPackageError("技能根目录不能是链接")
        for part in parts:
            path /= part
            if path.is_symlink():
                raise SkillPackageError("技能管理目录包含链接")
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise SkillPackageError("技能管理路径越界")
        return path

    def package(self, name: str) -> Path:
        if not isinstance(name, str) or len(name) > 64 or not NAME.fullmatch(name):
            raise SkillPackageError("技能名称无效")
        return self.checked(name)

    def operation_path(self, area: str, identity: str) -> Path:
        if area not in {"staging", "rollback", "journal"} or str(UUID(identity)) != identity:
            raise SkillPackageError("技能操作身份无效")
        return self.checked(f".management/{area}/{identity}" + (".json" if area == "journal" else ""))

    def initialize(self) -> None:
        if self.root.is_symlink():
            raise SkillPackageError("技能根目录不能是链接")
        self.root.mkdir(parents=True, exist_ok=True)
        directory = self.checked(".management")
        directory.mkdir(exist_ok=True, mode=0o700)
        marker = self.checked(".management/owner.json")
        if marker.exists():
            if json.loads(marker.read_text()) != {"format": FORMAT}:
                raise SkillPackageError("技能管理目录不属于当前应用")
        else:
            if any(directory.iterdir()):
                raise SkillPackageError("技能管理目录缺少归属记录")
            self.write_json(marker, {"format": FORMAT})
        for area in ["staging", "rollback", "journal"]:
            self.checked(f".management/{area}").mkdir(exist_ok=True, mode=0o700)
        sync_directory(directory)

    @staticmethod
    def write_json(path: Path, value: dict) -> None:
        if path.exists() or path.is_symlink():
            raise SkillPackageError("管理记录已经存在，停止覆盖")
        temporary = path.with_name(path.name + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise SkillPackageError("管理记录不是普通文件")
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.truncate(0)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)

    def write_candidate(self, identity: str, inspection: PackageInspection) -> None:
        stage = self.operation_path("staging", identity)
        stage.mkdir(mode=0o700)
        self.write_json(stage / "owner.json", {"format": FORMAT, "operation_id": identity})
        candidate = stage / "candidate"
        candidate.mkdir()
        for relative, data in inspection.files.items():
            path = candidate / relative  # 已经通过导入器完整路径和重复检查。
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        for current, _, _ in os.walk(candidate, topdown=False):
            sync_directory(Path(current))
        sync_directory(stage)
        sync_directory(stage.parent)

    def read(self, directory: Path) -> PackageInspection:
        return inspect_package(list(self.read_files(directory).items()))

    def read_files(self, directory: Path) -> dict[str, bytes]:
        """有界读取原字节；导出和恢复允许元数据损坏，路径/链接边界保持不变。"""
        directory = self.checked(directory.relative_to(self.root).as_posix())
        if not directory.is_dir():
            raise SkillPackageError("技能包目录不存在")
        files, total = [], 0
        for current, directories, names in os.walk(directory, followlinks=False):
            if any((Path(current) / name).is_symlink() for name in directories):
                raise SkillPackageError("技能包包含链接")
            for name in sorted(names):
                path = Path(current) / name
                data = _bytes(path, directory)
                total += len(data)
                if total > MAX_BUNDLE_BYTES:
                    raise SkillPackageError("技能包大小超限")
                files.append((path.relative_to(directory).as_posix(), data))
                if len(files) > MAX_BUNDLE_FILES:
                    raise SkillPackageError("技能包文件数量超限")
        return _checked_files(files)

    def move(self, source: Path, target: Path) -> None:
        source = self.checked(source.relative_to(self.root).as_posix())
        target = self.checked(target.relative_to(self.root).as_posix())
        if target.exists():
            raise SkillPackageError("技能替换目标已存在，停止覆盖")
        source.rename(target)
        sync_directory(source.parent)
        if target.parent != source.parent:
            sync_directory(target.parent)

    def remove_owned(self, path: Path) -> None:
        """只接受管理子树的确定路径；不跟随链接、不接收根包或调用方任意路径。"""
        relative = path.relative_to(self.root).as_posix()
        if not relative.startswith((".management/staging/", ".management/rollback/")):
            raise SkillPackageError("不能清理非管理目录")
        path = self.checked(relative)
        if not path.exists():
            return
        for current, directories, files in os.walk(path, followlinks=False):
            for name in [*directories, *files]:
                mode = (Path(current) / name).lstat().st_mode
                if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                    raise SkillPackageError("管理目录包含链接或特殊文件，保留待检查")
        shutil.rmtree(path)
        sync_directory(path.parent)
