"""回收已证明由程序安装、但DB从未成功登记的不可变对象。"""

import fcntl
import json
import os
import re
import stat
from heapq import nsmallest
from typing import TypedDict

from sqlalchemy import select

from knowledgeops_agent.app.lifecycle.files import _unlink
from knowledgeops_agent.persistence import models as db
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.persistence.installations import FORMAT, open_directory, open_gate, validate


class InstallationSweep(TypedDict):
    removed: int
    registered: int
    protected: int
    unrecognized: int
    next_cursor: str | None
    busy: bool


def _registered(session, root, kind, key):
    store = session.info.get("source_store" if kind == "source" else "file_store")
    if store is None or store.root != root:
        raise ValueError("installation root does not match configured storage")
    model, field = {
        "source": (db.SourceVersion, db.SourceVersion.storage_key),
        "delivery": (db.DeliveredFile, db.DeliveredFile.storage_key),
        "artifact": (db.RunArtifact, db.RunArtifact.content_storage_key),
        "knowledge": (db.KnowledgeVersion, db.KnowledgeVersion.body_storage_key),
        "profile": (db.LearningProfileRevision, db.LearningProfileRevision.storage_key),
        "profile_import": (db.LearningProfileImport, db.LearningProfileImport.storage_key),
        "collaboration": (db.LearningCollaboration, db.LearningCollaboration.storage_key),
        "file_operation": (db.FileOperation, db.FileOperation.content_key),
    }[kind]
    return session.scalar(select(field).select_from(model).where(field == key).limit(1)) is not None


def collect_installations(session, root, *, now, grace_seconds=86400, after=None, limit=100):
    """调用方持控制锁。只处理有限日志，不枚举/猜测历史内容目录，不删除DB记录。

    登记成功只释放日志；没有DB身份才校验哈希并删文件。删后崩溃会留下同一日志，
    下次缺文件按幂等处理。取不到目录独占锁就稍后重试，不等待拿控制锁的写入者。
    """
    if grace_seconds < 1 or limit < 1:
        raise ValueError("positive installation grace and batch required")
    root = root.resolve()
    result: InstallationSweep = {"removed": 0, "registered": 0, "protected": 0, "unrecognized": 0,
                                 "next_cursor": None, "busy": False}
    try:
        directory = open_directory(root)
    except FileNotFoundError:
        return result
    try:
        gate = open_gate(directory)
        try:
            try:
                fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {**result, "busy": True, "next_cursor": after}
            with os.scandir(directory) as entries:
                names = nsmallest(limit + 1, (item.name for item in entries
                    if re.fullmatch(r"[0-9a-f]{32}\.json", item.name) and (after is None or item.name > after)))
            store = FileStore(root)
            for name in names[:limit]:
                try:
                    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    with os.fdopen(descriptor, "rb") as stream:
                        before = os.fstat(stream.fileno())
                        if not stat.S_ISREG(before.st_mode):
                            raise ValueError("installation record is not a regular file")
                        if before.st_mtime > now.timestamp() - grace_seconds:
                            result["protected"] += 1
                            continue
                        record = json.loads(stream.read(4097))
                    if record.get("format") != FORMAT:
                        raise ValueError("unknown installation record")
                    kind, key, digest = record.get("kind"), record.get("key"), record.get("sha256")
                    validate(kind, key, digest)
                    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) != (
                        current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size,
                    ):
                        raise ValueError("installation record changed during collection")
                    if _registered(session, root, kind, key):
                        result["registered"] += 1
                    else:
                        result["removed"] += int(_unlink(store, key, digest))
                    os.unlink(name, dir_fd=directory)
                except FileNotFoundError:
                    pass
                except (ValueError, OSError, TypeError, AttributeError):
                    result["unrecognized"] += 1
            result["next_cursor"] = names[limit - 1] if len(names) > limit else None
        finally:
            os.close(gate)
    finally:
        os.close(directory)
    return result
