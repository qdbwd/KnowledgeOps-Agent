"""不可变文件安装日志：先登记再落盘，事务退出后才允许检查是否成为孤立对象。

日志仅证明本次程序安装的归属，不把旧目录里的文件自动纳管。目录共享锁覆盖
写入及DB提交；回收器非阻塞取独占锁，避免文件锁与业务控制锁倒序等待。
"""

import fcntl
import json
import os
import re
import stat
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.orm import Session

DIRECTORY = ".knowledgeops-content"
FORMAT = "knowledgeops-content-installation-v1"
_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_SHA = r"[0-9a-f]{64}"
PATTERNS = {
    "source": rf"[0-9a-f]{{2}}/{_SHA}",
    "delivery": rf"deliveries/blobs/[0-9a-f]{{2}}/{_SHA}",
    "artifact": rf"artifacts/{_ID}/{_SHA}\.txt",
    "knowledge": rf"knowledge/versions/{_ID}\.md",
    "profile": rf"profiles/versions/{_ID}/[1-9][0-9]*-{_SHA}\.md",
    "profile_import": rf"profiles/imports/{_ID}\.md",
    "collaboration": rf"learning/collaboration/{_ID}\.json",
    "file_operation": rf"artifacts/file-operations/{_ID}/content",
}


def validate(kind, key, digest):
    if not isinstance(key, str) or not isinstance(digest, str) or not isinstance(kind, str):
        raise ValueError("invalid content installation metadata")
    if not re.fullmatch(_SHA, digest) or not re.fullmatch(PATTERNS.get(kind, r"(?!)"), key):
        raise ValueError("content installation is outside its managed identity")
    if kind in {"source", "delivery"} and key.split("/")[-2:] != [digest[:2], digest]:
        raise ValueError("content installation hash differs from its address")
    if kind == "artifact" and not key.endswith(f"/{digest}.txt"):
        raise ValueError("artifact installation hash differs from its address")


def open_directory(root, *, create=False):
    """所有层级固定且不跟随符号链接；调用方负责关闭返回的目录描述符。"""
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if create:
            try:
                os.mkdir(DIRECTORY, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
        return os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)


def open_gate(directory):
    descriptor = os.open("lease.lock", os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=directory)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError("content installation gate is not a regular file")
    return descriptor


class InstallationLease:
    def __init__(self, store):
        self.store, self.seen = store, set()
        self.directory = open_directory(store.root, create=True)
        try:
            self.gate = open_gate(self.directory)
            try:
                fcntl.flock(self.gate, fcntl.LOCK_SH)
            except BaseException:
                os.close(self.gate)
                raise
        except BaseException:
            os.close(self.directory)
            raise

    def record(self, kind, key, digest):
        validate(kind, key, digest)
        if (kind, key) in self.seen:
            return
        # 已存在且没有日志的旧文件不能仅因这次复用便获得自动删除资格。
        # 即使复用仍持共享锁，保护旧失败安装的日志不会在本事务提交前被回收。
        if (self.store.root / key).exists() or (self.store.root / key).is_symlink():
            self.seen.add((kind, key))
            return
        name = uuid4().hex + ".json"
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=self.directory)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps({"format": FORMAT, "kind": kind, "key": key, "sha256": digest}).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(self.directory)
        self.seen.add((kind, key))

    def close(self):
        if self.directory is not None:
            os.close(self.gate)
            os.close(self.directory)
            self.directory = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def record_installation(session, store, kind, key, digest):
    """按根事务持有租约；回滚/Session关闭也释放，日志留给下一轮实际DB核对。"""
    if not session.in_transaction():
        raise RuntimeError("content installation requires a database transaction")
    leases = session.info.setdefault("content_installation_leases", {})
    root = str(store.root)
    if root not in leases:
        leases[root] = InstallationLease(store)
    leases[root].record(kind, key, digest)


@event.listens_for(Session, "after_transaction_end")
def _release_installations(session, transaction):
    # Flush内部事务和savepoint结束不代表根事务已经提交，不能提前释放。
    if transaction.parent is None:
        for lease in session.info.pop("content_installation_leases", {}).values():
            lease.close()
