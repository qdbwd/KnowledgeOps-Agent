"""工作目录中的不可变正文存储，数据库只保存相对地址和校验值。

先将文件写入并 fsync，再提交引用它的数据库事务。事务回滚可能留下孤立文件，
但不会提交指向尚未写成文件的版本。受管写入登记安装日志，孤立对象另经实际DB核对回收。
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath

from sqlalchemy.orm import object_session

from knowledgeops_agent.persistence.temporary import ManagedTemporary


class FileStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        """所有持久记录使用相对 POSIX 键；拒绝越界和符号链接逃逸。"""
        parsed = PurePosixPath(key)
        if not key or parsed.is_absolute() or ".." in parsed.parts or "\\" in key:
            raise ValueError("invalid workspace storage key")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("workspace path escapes storage root")
        return path

    def put(self, key: str, content: str, *, session=None, purpose=None) -> str:
        """原子写入 UTF-8 不可变文件；相同键仅允许相同内容，返回 SHA256。"""
        return self.put_bytes(key, content.encode("utf-8"), session=session, purpose=purpose)

    def put_bytes(self, key: str, data: bytes, *, session=None, purpose=None) -> str:
        """受管正文/原件显式传用途和事务，普通用户工作文件不自动纳入回收。"""
        digest = hashlib.sha256(data).hexdigest()
        if session is not None:
            from knowledgeops_agent.persistence.installations import record_installation

            record_installation(session, self, purpose, key, digest)
        elif purpose is not None:
            raise ValueError("managed file installation requires its transaction")
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError("immutable workspace file content mismatch")
            return digest
        with ManagedTemporary(self.root, kind="atomic") as temporary:
            temporary.stream.write(data)
            temporary.stream.flush()
            os.fsync(temporary.stream.fileno())
            # 链接创建是不可覆盖的原子安装；目标文件系统须支持硬链接。
            # 同键竞争若已存在，仍校验内容，不能覆盖另一写入者。
            try:
                os.link(temporary.path, target)
            except FileExistsError:
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise ValueError("immutable workspace file content mismatch") from None
        return digest

    def replace(self, key: str, data: bytes) -> None:
        """原子替换工作文件/可重建视图，保留权限；不可变正文必须调用 put。"""
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        with ManagedTemporary(self.root, kind="atomic") as temporary:
            if target.is_file():
                os.fchmod(temporary.stream.fileno(), stat.S_IMODE(target.stat().st_mode))
            temporary.stream.write(data)
            temporary.stream.flush()
            os.fsync(temporary.stream.fileno())
            os.replace(temporary.path, target)

    def adopt(self, temporary: Path, key: str, *, consume: bool = True, session=None, purpose=None) -> str:
        """接收流式写好的文件；批次安装可保留暂存到元数据提交，以便重试安装。"""
        if temporary.is_symlink():
            raise ValueError("stream artifact must not be a symbolic link")
        temporary = temporary.resolve()
        if not temporary.is_relative_to(self.root):
            raise ValueError("stream artifact must be inside its workspace")
        with temporary.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if session is not None:
            from knowledgeops_agent.persistence.installations import record_installation

            record_installation(session, self, purpose, key, digest)
        elif purpose is not None:
            raise ValueError("managed file installation requires its transaction")
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(temporary, target)
        except FileExistsError:
            with target.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                    raise ValueError("immutable stream artifact content mismatch") from None
        if consume:
            temporary.unlink()
        return digest

    def read(self, key: str, digest: str | None) -> str:
        """缺文件/内容被外部改写时明确失败，不能回退到旧数据库文本蒙混。"""
        data = self.path(key).read_bytes()
        if digest and hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"workspace file integrity mismatch: {key}")
        return data.decode("utf-8")


def stored_text(row, key: str | None, digest: str | None, legacy: str) -> str:
    """ORM 正文属性通过所属 Session 注入的 FileStore 读取；旧数据暂时兼容。

    不扫描环境或任意目录，也不把文件客户端放进 AgentState。读取发生在业务
    Session 生命周期内；调用方须在关闭 Session 前生成 DTO。
    """
    if not key:
        return legacy
    session = object_session(row)
    store = session.info.get("file_store") if session is not None else None
    if store is None:
        raise RuntimeError("file-backed data requires a session configured with FileStore")
    return store.read(key, digest)
