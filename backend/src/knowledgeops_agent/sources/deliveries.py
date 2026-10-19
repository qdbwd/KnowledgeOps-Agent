"""工作文件交付成固定用户资产，工具重放不会交付另一份已被修改的内容。"""

import fcntl
import hashlib
import json
import mimetypes
import os
from contextlib import ExitStack
from pathlib import Path

from sqlalchemy import select

from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.persistence.installations import InstallationLease
from knowledgeops_agent.persistence.models import DeliveredFile, OperationReceipt, new_id
from knowledgeops_agent.persistence.temporary import ManagedTemporary

MAX_FILE_BYTES = 100 * 1024 * 1024
PREVIEW_BYTES = 2 * 1024 * 1024


def _stamp(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def hold_blob(store, temporary, key, stack):
    """安装后持共享文件锁直到登记完成；回收器须取得独占锁才可移除同一对象。

    大文件复制与校验都在控制事务外。若上一轮GC刚移除了尚未登记的相同内容，
    暂存仍在，可重新安装；不能只拿着已被unlink的文件描述符去提交悬空路径。
    """
    # 大文件安装先于DB事务，共享租约交给调用者ExitStack，直到整批提交后释放。
    lease = stack.enter_context(InstallationLease(store))
    lease.record("delivery", key, key.rsplit("/", 1)[-1])
    for _ in range(3):
        store.adopt(temporary, key, consume=False)
        target = store.path(key)
        try:
            handle = target.open("rb")
        except FileNotFoundError:
            continue
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            if _stamp(os.fstat(handle.fileno())) == _stamp(target.stat()):
                stack.enter_context(handle)
                return
        except FileNotFoundError:
            pass
        except BaseException:
            handle.close()
            raise
        handle.close()
    raise ValueError("delivery storage changed during registration; retry this call")


def file_view(row: DeliveredFile) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "path": row.source_path,
        "bytes": row.size_bytes,
        "sha256": row.sha256,
        "mime_type": row.mime_type,
        "run_id": row.run_id,
        "root_id": row.root_id,
        "created_at": row.created_at.isoformat(),
        "url": f"/api/deliveries/{row.id}/download",
        "view_url": f"/deliveries/{row.id}",
        "deleted": row.deleted_at is not None,
    }


class DeliveryService:
    def __init__(self, sessions, root: Path):
        self.sessions, self.store = sessions, FileStore(root)

    @staticmethod
    def _receipt(session, operation_key, request_hash):
        receipt = session.get(OperationReceipt, operation_key)
        if receipt is None:
            return None
        if receipt.request_hash != request_hash:
            raise ValueError("delivery call was already used for a different request")
        return receipt.result["files"]

    def present(
        self,
        paths: list[str],
        *,
        run_id: str,
        root_id: str,
        call_id: str,
        guard=lambda: None,
        commit_guard=None,
    ) -> list[dict]:
        """先流式复制与核对，后用短事务登记整批资产和幂等回执。

        参数相同的旧调用直接重放，工作文件后来被删除也不影响旧结果。守卫在复制中
        检查取消，生产工具另传入与事务共用的执行守卫，使取消与提交有确定先后。
        """
        from knowledgeops_agent.harness.sandbox.tools import workspace_file

        if not call_id or not 1 <= len(paths) <= 20 or len(set(paths)) != len(paths):
            raise ValueError("a stable call id and 1 to 20 distinct file paths are required")
        request_hash = hashlib.sha256(json.dumps(paths, ensure_ascii=False).encode()).hexdigest()
        operation_key = "delivery:" + hashlib.sha256(f"{run_id}\0{call_id}".encode()).hexdigest()
        guard()
        with self.sessions() as session:
            prior = self._receipt(session, operation_key, request_hash)
            if prior is not None:
                return prior
        targets = [workspace_file(self.store.root, path, delivery=True) for path in paths]
        with ExitStack() as stack:
            staged = []
            for target in targets:
                guard()
                temporary = stack.enter_context(ManagedTemporary(self.store.root, kind="atomic"))
                hasher, size = hashlib.sha256(), 0
                with target.open("rb") as source:
                    before = _stamp(os.fstat(source.fileno()))
                    while data := source.read(1024 * 1024):
                        guard()
                        size += len(data)
                        if size > MAX_FILE_BYTES:
                            raise ValueError("delivery file exceeds 100 MiB")
                        temporary.stream.write(data)
                        hasher.update(data)
                    if before != _stamp(os.fstat(source.fileno())) or before != _stamp(
                        target.stat()
                    ):
                        raise ValueError(
                            "workspace file changed during delivery; read the current file again"
                        )
                temporary.stream.flush()
                os.fsync(temporary.stream.fileno())
                temporary.stream.close()
                staged.append((target, temporary.path, hasher.hexdigest(), size))
            guard()
            for _, temporary, digest, _ in staged:
                hold_blob(self.store, temporary, f"deliveries/blobs/{digest[:2]}/{digest}", stack)
            with self.sessions.begin() as session:
                session.info["delivery_store"] = self.store
                lock_control(session)
                if commit_guard is not None:
                    commit_guard(session)
                prior = self._receipt(session, operation_key, request_hash)
                if prior is not None:
                    return prior
                rows = []
                for target, _temporary, digest, size in staged:
                    key = f"deliveries/blobs/{digest[:2]}/{digest}"
                    row = DeliveredFile(
                        id=new_id(),
                        run_id=run_id,
                        root_id=root_id,
                        name=target.name,
                        source_path="/" + target.relative_to(self.store.root).as_posix(),
                        storage_key=key,
                        sha256=digest,
                        size_bytes=size,
                        mime_type=mimetypes.guess_type(target.name)[0]
                        or "application/octet-stream",
                    )
                    session.add(row)
                    rows.append(row)
                session.flush()
                result = [file_view(row) for row in rows]
                session.add(
                    OperationReceipt(
                        operation_key=operation_key,
                        request_hash=request_hash,
                        result={"files": result},
                    )
                )
                return result

    def list(self, *, root_id: str | None = None) -> list[dict]:
        query = select(DeliveredFile).where(DeliveredFile.deleted_at.is_(None))
        if root_id:
            query = query.where(DeliveredFile.root_id == root_id)
        with self.sessions() as session:
            return [
                file_view(row)
                for row in session.scalars(query.order_by(DeliveredFile.created_at.desc()))
            ]

    def get(self, identity: str) -> dict:
        with self.sessions() as session:
            row = session.get(DeliveredFile, identity)
            if row is None:
                raise KeyError("delivered file not found")
            return file_view(row)

    def read(self, identity: str) -> tuple[dict, bytes]:
        with self.sessions() as session:
            row = session.get(DeliveredFile, identity)
            if row is None:
                raise KeyError("delivered file not found")
            if not 0 <= row.size_bytes <= MAX_FILE_BYTES:
                raise ValueError("delivered file integrity metadata is invalid")
            with self.store.path(row.storage_key).open("rb") as stream:
                data = stream.read(row.size_bytes + 1)
            if hashlib.sha256(data).hexdigest() != row.sha256 or len(data) != row.size_bytes:
                raise ValueError("delivered file integrity check failed")
            return file_view(row), data

    def preview(self, identity: str) -> dict:
        view = self.get(identity)
        if view["bytes"] > PREVIEW_BYTES:
            return {**view, "text": None, "reason": "此文件较大，请下载后查看。"}
        view, data = self.read(identity)
        try:
            if b"\0" in data:
                raise UnicodeError()
            content = data.decode("utf-8-sig")
        except UnicodeError:
            return {**view, "text": None, "reason": "此文件为二进制或非UTF-8格式，请下载后查看。"}
        return {
            **view,
            "text": content,
            "markdown": Path(view["name"]).suffix.lower() in {".md", ".markdown"},
        }
