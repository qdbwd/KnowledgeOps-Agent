"""文件版本门与持久修改回执。

读取标记扫描及同步拦截流程适配 DeerFlow read_before_write_middleware.py
8d994cc1 (MIT，许可见 vendor/deerflow/LICENSE)。本项目保留 Deep Agents
工具和参数，增加 PG 回执、原子替换与压缩视图检查；检查失败不会放行。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import DeleteResult, EditResult, WriteResult
from deepagents.backends.utils import perform_string_replacement
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from sqlalchemy import literal, or_, select

from knowledgeops_agent.harness.context.management import working_messages
from knowledgeops_agent.harness.permissions.receipts import ExecutionUnknown, execution_fingerprint
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.persistence.models import FileOperation, new_id, utcnow

READ_MARK_KEY = "knowledgeops_file_read"
_WRITES = {"write_file", "edit_file", "delete"}
_condition = threading.Condition()
_active_paths: set[tuple[str, str]] = set()


@contextmanager
def path_lock(workspace: str, path: str):
    """相同或祖先/后代路径互斥，独立文件可并行；不约束外部编辑器。"""
    key = (workspace, path)
    with _condition:
        _condition.wait_for(lambda: not any(
            workspace == root and (path == other or path.startswith(other + "/") or other.startswith(path + "/"))
            for root, other in _active_paths
        ))
        _active_paths.add(key)
    try:
        yield
    finally:
        with _condition:
            _active_paths.remove(key)
            _condition.notify_all()


def file_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError("可靠文件操作不跟随符号链接，请直接使用真实工作区路径")
    if path.is_dir():
        entries = []
        for child in sorted(path.rglob("*")):
            if child.is_symlink():
                raise ValueError("目录含符号链接，不能可靠核对删除范围")
            entries.append((child.relative_to(path).as_posix(), "directory" if child.is_dir() else file_hash(child)))
        return hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode()).hexdigest()
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _latest_mark(messages: list, path: str, workspace: str) -> dict | None:
    """适配上游 _latest_mark_hash；只扫描本轮模型仍能看到的消息。"""
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            continue
        mark = (message.additional_kwargs or {}).get(READ_MARK_KEY)
        if isinstance(mark, dict) and mark.get("path") == path and mark.get("workspace") == workspace:
            return mark
    return None


def _message(result, call_id):
    if isinstance(result, ToolMessage):
        return result
    if isinstance(getattr(result, "update", None), dict):
        return next((m for m in result.update.get("messages", [])
                     if isinstance(m, ToolMessage) and m.tool_call_id == call_id), None)
    return None


def _row_view(row: FileOperation) -> dict:
    return {key: getattr(row, key) for key in (
        "id", "root_id", "run_id", "tool_call_id", "fingerprint", "workspace", "path",
        "operation", "authority_epoch", "before_hash", "after_hash", "content_key",
        "status", "result", "revision",
    )}


@dataclass
class FileCall:
    context: Any
    call: dict
    state: dict
    journal: Any
    read_verified: bool = False


current_file_call: ContextVar[FileCall | None] = ContextVar("knowledgeops_file_call", default=None)


class FileJournal:
    def __init__(self, context, backend):
        self.context = context
        self.backend = backend
        self.store = FileStore(backend.cwd)

    def find(self, call: dict) -> dict | None:
        fingerprint = execution_fingerprint(call, workspace=str(self.store.root), cwd=str(self.store.root))
        with self.context.jobs.sessions() as session:
            row = session.scalar(select(FileOperation).where(
                FileOperation.run_id == self.context.run_id, FileOperation.tool_call_id == call["id"],
            ))
            if row and row.fingerprint != fingerprint:
                raise PermissionError("同一文件调用的参数或工作区已变化，不能作为原调用重放")
            return _row_view(row) if row else None

    def mutation_token(self, path: str) -> str | None:
        with self.context.jobs.sessions() as session:
            return session.scalar(select(FileOperation.id).where(
                FileOperation.workspace == str(self.store.root),
                FileOperation.status.in_(["running", "applied", "sync_pending", "succeeded", "unknown", "conflict", "skipped"]),
                or_(FileOperation.path == path,
                    FileOperation.path.startswith(path + "/", autoescape=True),
                    literal(path).startswith(FileOperation.path + "/")),
            ).order_by(FileOperation.created_at.desc(), FileOperation.id.desc()).limit(1))

    def mark(self, path: Path) -> dict:
        key = path.relative_to(self.store.root).as_posix()
        return {"workspace": str(self.store.root), "path": key,
                "hash": file_hash(path), "mutation": self.mutation_token(key)}

    def require_read(self, scope: FileCall, path: Path) -> None:
        if not path.exists():
            return
        messages = working_messages(scope.state.get("messages", []), scope.state.get("context_compaction"))
        # 目录删除保留 SDK 能力，但目录列表与每个包含的文件都须先读取。
        targets = [path, *(p for p in sorted(path.rglob("*")) if p.is_file())] if path.is_dir() else [path]
        for target in targets:
            current = self.mark(target)
            if _latest_mark(messages, current["path"], current["workspace"]) != current:
                raise ValueError(
                    f"文件版本未核对或已变化：/{current['path']}。请重新读取当前版本后再修改；"
                    "目录删除前需 ls 目录并 read_file 其中的文件。修改或历史压缩后旧读取标记不再有效。"
                )

    def _sync_baseline(self, path: Path) -> dict:
        """只保存原版本身份，正文仍由不可变知识版本读取。"""
        from knowledgeops_agent.knowledge.files import published_files, worktree
        key = path.relative_to(self.store.root).as_posix()
        if not key.startswith("knowledge/documents/"):
            return {}
        known = published_files(self.context.jobs.sessions)
        paths = [p for p in worktree(self.context.jobs.sessions) if p == key or p.startswith(key + "/")]
        if path.suffix == ".md" and key not in paths:
            paths.append(key)
        return {"paths": paths, "versions": {
            p: {k: known[p][k] for k in ("document_id", "version_id", "title", "archived")}
            for p in paths if p in known
        }}

    def mutate(self, operation: str, path_string: str, **args):
        """由上游工具验证参数后进入；先提交意图再执行，未知结果绝不自动重做。"""
        scope = current_file_call.get()
        if scope is None:
            raise RuntimeError("文件修改缺少工具调用身份")
        path = self.backend._resolve_path(path_string)
        key = path.relative_to(self.store.root).as_posix()
        if key == ".":
            raise PermissionError("不能删除或覆盖整个工作区")
        row = self.find(scope.call)
        result_class = {"write_file": WriteResult, "edit_file": EditResult, "delete": DeleteResult}[operation]
        if row is None:
            self.require_read(scope, path)
            before = file_hash(path)
            result = result_class(path=path_string)
            content = None
            if operation == "write_file":
                if path.exists():
                    return WriteResult(error="文件已经存在；请读取后使用 edit_file 精确编辑。")
                content = args["content"]
            elif operation == "edit_file":
                if not path.is_file():
                    return EditResult(error="目标文件不存在或不是文本文件")
                normalized = [args[k].replace("\r\n", "\n").replace("\r", "\n") for k in ("old_string", "new_string")]
                replacement = perform_string_replacement(
                    path.read_text(encoding="utf-8"), normalized[0], normalized[1], args["replace_all"],
                )
                if isinstance(replacement, str):
                    return EditResult(error=replacement)
                content, occurrences = replacement
                result = EditResult(path=path_string, occurrences=occurrences)
            elif not path.exists():
                return DeleteResult(error="目标不存在")
            if path.is_dir() and any(self.backend._protected("/" + p.relative_to(self.store.root).as_posix()) for p in path.rglob("*")):
                raise PermissionError("目录包含应用管理区，不能递归删除")
            identifier = new_id()
            content_key = f"artifacts/file-operations/{identifier}/content" if content is not None else None
            after = self.store.put(content_key, content) if content_key and content is not None else None
            baseline = self._sync_baseline(path)
            with self.context.jobs.sessions.begin() as session:
                root = self.context.jobs._locked(session, self.context.root_id)
                entry = FileOperation(
                    id=identifier, root_id=root.id, run_id=self.context.run_id,
                    tool_call_id=scope.call["id"],
                    fingerprint=execution_fingerprint(scope.call, workspace=str(self.store.root), cwd=str(self.store.root)),
                    workspace=str(self.store.root), path=key, operation=operation,
                    authority_epoch=root.authority_epoch, before_hash=before, after_hash=after,
                    content_key=content_key, status="prepared",
                    result={"backend": asdict(result), "knowledge_baseline": baseline},
                )
                session.add(entry)
                session.flush()
                row = _row_view(entry)
        current = file_hash(path)
        if row["status"] == "prepared":
            if current != row["before_hash"]:
                self._unknown(row, "conflict", "文件在执行前已被其他编辑改变")
            self.context.jobs.guard(self.context.run_id, worker_id=self.context.worker_id)
            self._status(row, "running")
            if operation == "delete":
                # 原子移出工作树，保留删除内容供核对；不先递归删到一半。
                archived = self.store.path(f"artifacts/file-operations/{row['id']}/deleted")
                archived.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, archived)
            else:
                content = self.store.read(row["content_key"], row["after_hash"])
                if operation == "write_file":
                    self.store.put(key, content)
                else:
                    self.store.replace(key, content.encode("utf-8"))
            current = file_hash(path)
        elif row["status"] not in {"running", "applied", "sync_pending", "unknown", "conflict"}:
            raise ValueError("文件操作回执状态不允许执行")
        if current != row["after_hash"]:
            self._unknown(row, "unknown" if current == row["before_hash"] else "conflict",
                          "文件操作结果无法证明；保留现状，需核对原操作后再继续")
        self._status(row, "applied")
        return result_class(**row["result"]["backend"])

    def _status(self, row: dict, status: str):
        with self.context.jobs.sessions.begin() as session:
            entry = session.scalar(select(FileOperation).where(FileOperation.id == row["id"]).with_for_update())
            entry.status = status
            entry.revision += 1
            entry.updated_at = utcnow()

    def _unknown(self, row: dict, status: str, message: str):
        self._status(row, status)
        self.context.jobs.append_event(self.context.run_id, "file_operation_unknown", {
            "receipt_id": row["id"], "path": row["path"], "status": status,
        })
        raise ExecutionUnknown(message)

    def finish(self, call: dict, result) -> None:
        message = _message(result, call["id"])
        if message is None:
            return
        with self.context.jobs.sessions.begin() as session:
            row = session.scalar(select(FileOperation).where(
                FileOperation.run_id == self.context.run_id, FileOperation.tool_call_id == call["id"],
            ).with_for_update())
            if row is None:
                return
            row.result = {**(row.result or {}), "message": message.model_dump(mode="json")}
            sync = message.additional_kwargs.get("knowledgeops_file_sync", {})
            row.status = "sync_pending" if sync.get("status") == "sync_failed" else "succeeded"
            row.revision += 1
            row.updated_at = utcnow()


class FileReliabilityMiddleware(AgentMiddleware):
    def __init__(self, backend):
        self.backend = backend

    def wrap_tool_call(self, request, handler):
        call = request.tool_call
        if call["name"] not in {*_WRITES, "read_file", "ls"}:
            return handler(request)
        raw_path = call["args"].get("file_path", call["args"].get("path"))
        if not isinstance(raw_path, str) or raw_path.startswith("/skills/") or raw_path == "/skills":
            return handler(request)
        path = self.backend._resolve_path(raw_path)
        journal = FileJournal(request.runtime.context, self.backend)
        with path_lock(str(self.backend.cwd), path.as_posix()):
            if call["name"] in _WRITES:
                saved = journal.find(call)
                if saved and saved["status"] in {"succeeded", "skipped"}:
                    return ToolMessage.model_validate(saved["result"]["message"])
                scope = FileCall(request.runtime.context, call, request.state, journal)
                token = current_file_call.set(scope)
                try:
                    result = handler(request)
                    journal.finish(call, result)
                    return result
                finally:
                    current_file_call.reset(token)
            # 根目录不能作为 delete 目标，不为普通 ls 根目录递归扫描全部产物。
            if path == self.backend.cwd and call["name"] == "ls":
                return handler(request)
            before = journal.mark(path)
            scope = FileCall(request.runtime.context, call, request.state, journal)
            token = current_file_call.set(scope)
            try:
                result = handler(request)
            finally:
                current_file_call.reset(token)
            message = _message(result, call["id"])
            if (message is not None and message.status != "error" and before["hash"] is not None
                    and (call["name"] == "ls" or scope.read_verified)):
                if before != journal.mark(path):
                    raise ValueError("读取过程中内容已变化，请重新读取")
                message.additional_kwargs = {**message.additional_kwargs, READ_MARK_KEY: before}
            return result
