"""审批和实际文件写入共用的路径边界与只读版本事实。

沿 Deep Agents 虚拟根路径约定，补充本应用的受管目录与符号链接限制。
本模块不建目录、不写文件、不运行工具；读取内容只是待审数据，不构成授权。
"""

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath

MANAGED_DIRECTORIES = {"uploads", "artifacts", "deliveries", ".knowledgeops-temporary", ".knowledgeops-content",
                       ".knowledgeops-maintenance.lock"}
FILE_MUTATIONS = frozenset({"write_file", "edit_file", "delete"})
_TEXT_BYTES = 8192


def mutable_path(root: Path, raw_path: str) -> Path:
    """只允许虚拟根内普通工作文件；检查每级原路径，不能先resolve抹去链接。"""
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path or ".." in raw_path:
        raise PermissionError("文件路径无效或包含越界访问")
    relative = PurePosixPath(raw_path.lstrip("/"))
    parts = relative.parts
    if not parts or parts[0].startswith("~"):
        raise PermissionError("不能删除或覆盖整个工作区")
    if parts[0] in {*MANAGED_DIRECTORIES, "skills"} or (
        parts[0] == "knowledge" and (len(parts) < 2 or parts[1] != "documents")
    ):
        raise PermissionError("此管理区只读；知识正文请编辑 /knowledge/documents，普通文件使用 /work 或 /outputs")
    root = root.resolve()
    path = root
    for index, part in enumerate(parts):
        path = path / part
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise PermissionError("文件修改不跟随符号链接，请使用工作区真实路径")
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise PermissionError("文件修改不能操作命名管道、设备或其他特殊文件")
        if index < len(parts) - 1 and not stat.S_ISDIR(mode):
            raise PermissionError("文件路径的父级不是目录")
    if not path.resolve().is_relative_to(root):
        raise PermissionError("文件路径超出工作区")
    return path


def _version(info: os.stat_result) -> list[int]:
    # 不包含atime：核对读取本身不能让批准失效。ctime/ino可识别同内容换文件。
    return [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def file_target(root: Path, raw_path: str) -> dict:
    """完整散列绑定内容与身份，审查正文有界；目录保留完整成员版本清单。

    大目录的审查输入超过网关容量时沿统一服务转人工，不截断成员后假装覆盖全体。
    读取不锁住宿主编辑器；扫描中变化即失败，真正修改前仍须再次核对。
    """
    try:
        return _inspect_target(root, raw_path)
    except OSError as error:
        raise PermissionError("文件目标无法稳定读取，请核对后重新提出操作") from error


def _inspect_target(root: Path, raw_path: str) -> dict:
    path = mutable_path(root, raw_path)
    key = "/" + path.relative_to(root.resolve()).as_posix()
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {"path": key, "kind": "missing"}
    if stat.S_ISDIR(before.st_mode):
        members = [file_target(root, "/" + child.relative_to(root.resolve()).as_posix())
                   for child in sorted(path.iterdir())]
        # 子文件预览不是删除授权，只需完整路径/散列/身份；减轻目录审查背景。
        for child in members:
            child.pop("text", None)
            child.pop("text_complete", None)
        if _version(path.lstat()) != _version(before):
            raise PermissionError("文件目录在核对过程中变化，请重新读取后提出操作")
        return {"path": key, "kind": "directory", "version": _version(before), "members": members}
    # O_NONBLOCK避免竞态下替换成FIFO导致线程永久阻塞，NOFOLLOW避免同类链接竞态。
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or _version(opened) != _version(before):
            raise PermissionError("文件在核对过程中变化，请重新读取后提出操作")
        preview = stream.read(_TEXT_BYTES + 1)
        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if _version(os.fstat(stream.fileno())) != _version(before) or _version(path.lstat()) != _version(before):
            raise PermissionError("文件在核对过程中变化，请重新读取后提出操作")
    try:
        text = preview[:_TEXT_BYTES].decode("utf-8")
    except UnicodeDecodeError:
        text = None
    return {"path": key, "kind": "file", "version": _version(before), "sha256": digest,
            "bytes": before.st_size, "text": text,
            "text_complete": text is not None and len(preview) <= _TEXT_BYTES}
