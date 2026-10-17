"""受管内部材料的文件删除边界；调用方先在事务中验证归属、引用与清理资格。"""

import hashlib
import json
import os
import re
import stat
from contextlib import ExitStack
from uuid import UUID


def _identity(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("internal file owner must be a canonical UUID")
    return value


def _digest(value: str | None) -> str:
    if not value or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("verified internal file digest required")
    return value


def _unlink(store, key: str, digest: str) -> bool:
    """用目录描述符遍历，任何一层符号链接都拒绝；不会跟随链接删除其他文件。

    仅Linux开发/worker执行。程序内写入还需由调用方持有workspace_lock；这不
    宣称能锁住宿主编辑器。缺失是幂等成功，其他I/O或内容差异必须保留重试。
    """
    parts = key.split("/")
    with ExitStack() as stack:
        directory = os.open(store.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, directory)
        try:
            for component in parts[:-1]:
                directory = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                stack.callback(os.close, directory)
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return False
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("internal cleanup only removes regular files")
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("internal file changed; cleanup requires review")
            current = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns,
            ):
                raise ValueError("internal file changed during cleanup")
            os.unlink(parts[-1], dir_fd=directory)
    return True


def remove_artifact_file(store, *, run_id: str, content_hash: str, storage_key: str) -> bool:
    """只接受RunArtifact的既有生成地址，不能用DB里的任意路径删除用户资产。"""
    expected = f"artifacts/{_identity(run_id)}/{_digest(content_hash)}.txt"
    if storage_key != expected:
        raise ValueError("artifact storage key is outside its managed identity")
    return _unlink(store, expected, content_hash)


def remove_operation_file(store, *, operation_id: str, digest: str, deleted: bool = False) -> bool:
    """只清理文件操作的不可变回执/删除前副本，不碰用户当前工作路径。"""
    key = f"artifacts/file-operations/{_identity(operation_id)}/{'deleted' if deleted else 'content'}"
    return _unlink(store, key, _digest(digest))


def operation_tree_manifest(store, operation_id: str, digest: str) -> list[list[str]] | None:
    """目录删除的恢复副本先核验原树哈希，再把文件身份清单交给作业持久化。

    None表示目标为普通文件或已缺失。目录内容清单只有路径/哈希，不复制正文；
    必须先提交清单，才逐批移除，否则部分删除后的树无法再与原始整体哈希比较。
    """
    key = f"artifacts/file-operations/{_identity(operation_id)}/deleted"
    with ExitStack() as stack:
        directory = os.open(store.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, directory)
        try:
            for part in key.split("/")[:-1]:
                directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                stack.callback(os.close, directory)
            kind = os.stat("deleted", dir_fd=directory, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return None
        if stat.S_ISREG(kind):
            return None
        if not stat.S_ISDIR(kind):
            raise ValueError("operation archive is not a regular file or directory")
        archive = os.open("deleted", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        stack.callback(os.close, archive)
        entries: list[list[str]] = []
        for parent, dirs, files, descriptor in os.fwalk(".", dir_fd=archive, follow_symlinks=False):
            for name in dirs + files:
                relative = name if parent == "." else f"{parent[2:]}/{name}"
                mode = os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode):
                    value = "directory"
                elif stat.S_ISREG(mode):
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                    with os.fdopen(fd, "rb") as stream:
                        value = hashlib.file_digest(stream, "sha256").hexdigest()
                else:
                    raise ValueError("operation archive contains links or special files")
                entries.append([relative, value])
                if len(entries) > 10000:
                    raise ValueError("operation archive exceeds bounded manifest size")
        entries.sort()
        actual = hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode()).hexdigest()
        if actual != _digest(digest):
            raise ValueError("operation archive tree changed; cleanup requires review")
        return list(reversed(entries)) + [[".", "directory"]]


def remove_operation_tree_entry(store, operation_id: str, entry: list[str]) -> bool:
    """只处理已提交清单的一项；逐叶哈希验证，目录用rmdir，新增文件会阻止完成。"""
    relative, value = entry
    if relative != "." and (not relative or "\\" in relative or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise ValueError("invalid operation archive entry")
    key = f"artifacts/file-operations/{_identity(operation_id)}/deleted"
    if relative != ".":
        key += "/" + relative
    if value != "directory":
        return _unlink(store, key, _digest(value))
    with ExitStack() as stack:
        directory = os.open(store.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, directory)
        try:
            for part in key.split("/")[:-1]:
                directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                stack.callback(os.close, directory)
            leaf = key.split("/")[-1]
            if not stat.S_ISDIR(os.stat(leaf, dir_fd=directory, follow_symlinks=False).st_mode):
                raise ValueError("operation archive directory changed")
            os.rmdir(leaf, dir_fd=directory)
        except FileNotFoundError:
            return False
    return True
