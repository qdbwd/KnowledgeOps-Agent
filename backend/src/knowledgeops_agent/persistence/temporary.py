"""受管暂存的文件租约：锁随进程退出释放，登记只保存类型/拥有者，不保存正文。

原子写入成功后删除登记；崩溃留下的文件由维护器在宽限期后回收。路径由固定
目录和随机身份生成，元数据不能指定任意删除地址。本项目仅在Linux容器执行。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
from datetime import datetime
from heapq import nsmallest
from pathlib import Path
from typing import TypedDict
from uuid import UUID, uuid4

DIRECTORY = ".knowledgeops-temporary"
KINDS = {"atomic", "upload", "command-output"}


class TemporarySweep(TypedDict):
    removed: int
    protected: int
    unrecognized: int
    next_cursor: str | None


class ManagedTemporary:
    def __init__(self, root: Path, *, kind: str, owner_id: str | None = None):
        if kind not in KINDS or (kind == "command-output" and not owner_id):
            raise ValueError("invalid managed temporary owner")
        if owner_id and str(UUID(owner_id)) != owner_id:
            raise ValueError("temporary owner must be a canonical UUID")
        directory = root / DIRECTORY
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink():
            raise ValueError("temporary directory cannot be a symbolic link")
        identity = uuid4().hex
        self.path, self.record = directory / f"{identity}.part", directory / f"{identity}.json"
        self.lease = self.record.open("xb")
        fcntl.flock(self.lease.fileno(), fcntl.LOCK_EX)
        try:
            self.lease.write(json.dumps({"format": "knowledgeops-temporary-v1", "kind": kind, "owner_id": owner_id}).encode())
            self.lease.flush()
            os.fsync(self.lease.fileno())
            self.stream = self.path.open("xb", buffering=0)
        except BaseException:
            self.record.unlink(missing_ok=True)
            self.lease.close()
            raise

    def close(self, *, remove: bool = True):
        """remove=False保留失败捕获，释放活锁；GC仍需检查拥有者的恢复生命周期。"""
        if self.lease.closed:
            return
        try:
            self.stream.close()
            if remove:
                self.path.unlink(missing_ok=True)
                self.record.unlink(missing_ok=True)
        finally:
            self.lease.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _regular_stamp(directory, name):
    try:
        value = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("temporary entry is not a regular file")
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _collect_one(directory, name, *, cutoff, owner_released):
    """持有登记文件锁到两份文件移除结束，避免仍在安装正式文件的写入者被回收。"""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("temporary lease is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        record = _regular_stamp(directory, name)
        payload = name[:-5] + ".part"
        content = _regular_stamp(directory, payload)
        if record is None or max(record[3], content[3] if content else 0) / 1e9 > cutoff:
            return False
        metadata = json.loads(stream.read(4097))
        if metadata.get("format") != "knowledgeops-temporary-v1" or metadata.get("kind") not in KINDS:
            raise ValueError("unknown temporary metadata")
        owner = metadata.get("owner_id")
        if metadata["kind"] == "command-output" and not owner:
            raise ValueError("command capture has no owner")
        if owner and not owner_released(owner):
            return False
        if record != _regular_stamp(directory, name) or content != _regular_stamp(directory, payload):
            raise ValueError("temporary entry changed during cleanup")
        if content is not None:
            os.unlink(payload, dir_fd=directory)
        os.unlink(name, dir_fd=directory)
        return True


def collect_temporaries(root: Path, *, now: datetime, grace_seconds: int = 86400,
                        owner_released=lambda _: False, after: str | None = None, limit: int = 100) -> TemporarySweep:
    """只枚举固定登记目录；一批只处理limit项，未知/损坏登记保留并计数。"""
    if grace_seconds < 1 or limit < 1:
        raise ValueError("positive temporary grace and batch required")
    result: TemporarySweep = {"removed": 0, "protected": 0, "unrecognized": 0, "next_cursor": None}
    try:
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return result
    try:
        try:
            directory = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        except FileNotFoundError:
            return result
    finally:
        os.close(parent)
    try:
        with os.scandir(directory) as entries:
            names = nsmallest(limit + 1, (item.name for item in entries
                if re.fullmatch(r"[a-f0-9]{32}\.json", item.name) and (after is None or item.name > after)))
        for name in names[:limit]:
            try:
                removed = _collect_one(directory, name, cutoff=now.timestamp() - grace_seconds, owner_released=owner_released)
                if removed:
                    result["removed"] += 1
                else:
                    result["protected"] += 1
            except FileNotFoundError:
                pass  # 另一个维护者已完成，按幂等处理。
            except (ValueError, OSError, TypeError, AttributeError):
                result["unrecognized"] += 1
        result["next_cursor"] = names[limit - 1] if len(names) > limit else None
    finally:
        os.close(directory)
    return result
