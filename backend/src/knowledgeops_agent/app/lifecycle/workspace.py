"""清理前核对用户工作文件的显式内部链接；不删除这些文件，也不扫描宿主其他目录。"""

import hashlib
import os
import re
import stat

from sqlalchemy import delete

from knowledgeops_agent.app.lifecycle.references import replace_references
from knowledgeops_agent.persistence.models import ArtifactRetentionReference

_LINK = re.compile(rb"artifacts/[0-9a-f-]{36}/[0-9a-f]{64}\.txt|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
# 这些不可变正文/原件由DB资产引用负责，运行内部材料由引用图负责。
_MANAGED = {"artifacts", "uploads", "profiles", "learning", ".home", ".knowledgeops-temporary", "knowledge/versions", "knowledge/current"}


def refresh_workspace_references(session, store) -> dict:
    """调用方持workspace锁和控制锁。流式查找ID/内部路径，不把整文件复制进PG。

    不跟随符号链接或特殊设备；扫描容量超出时失败保护，不能把部分结果当完整。
    宿主外部编辑无法被应用锁阻止，读取前后检查文件元数据发现变化则留待重试。
    """
    if store is None:
        return {"files": 0}
    seen: set[str] = set()
    total = 0
    for parent, dirs, files, directory in os.fwalk(store.root, follow_symlinks=False):
        base = os.path.relpath(parent, store.root).replace(os.sep, "/")
        def key(name, base=base):
            return name if base == "." else f"{base}/{name}"
        dirs[:] = [name for name in dirs if key(name) not in _MANAGED]
        for name in dirs:
            if (store.root / key(name)).is_symlink():
                raise ValueError("workspace reference scan does not follow directory links")
        for name in files:
            path_key = key(name)
            if base == "." and name == ".knowledge-write.lock":
                continue
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            matches: set[str] = set()
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("workspace reference scan requires regular files")
                total += before.st_size
                if len(seen) >= 10000 or total > 256 * 1024 * 1024:
                    raise ValueError("workspace reference scan exceeds its bounded pass")
                tail = b""
                while chunk := stream.read(65536):
                    data = tail + chunk
                    matches.update(match.group().decode("ascii") for match in _LINK.finditer(data))
                    tail = data[-128:]
                after = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                ):
                    raise ValueError("workspace file changed during reference scan")
            identity = hashlib.sha256(path_key.encode()).hexdigest()
            replace_references(session, "workspace", identity, sorted(matches))
            seen.add(identity)
    session.execute(delete(ArtifactRetentionReference).where(
        ArtifactRetentionReference.owner_kind == "workspace", ArtifactRetentionReference.owner_id.not_in(seen),
    ))
    return {"files": len(seen)}
