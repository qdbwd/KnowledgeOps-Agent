"""通用文件编辑后的知识同步：文件操作自由，版本/引用/索引由原业务服务维护。

documents 是可编辑工作树，versions 是已发布正文的不可变快照。未获发布许可、
校验失败或崩溃时保留工作文件和草稿，不用旧正文覆盖用户刚完成的编辑。
"""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from sqlalchemy import select

from knowledgeops_agent.db import ChangeSet, KnowledgeDocument, KnowledgeVersion

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
_local = threading.local()


@contextmanager
def workspace_lock(sessions):
    """串行应用内文件修改与发布；锁文件不代表对宿主编辑器的强制锁。"""
    store = sessions.kw.get("info", {}).get("file_store")
    if store is None:
        yield
        return
    import fcntl

    key = str(store.root)
    with _locks_guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        held: set[str] = getattr(_local, "held", set())
        if key in held:
            yield
            return
        with (store.root / ".knowledge-write.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            _local.held = held | {key}
            try:
                yield
            finally:
                _local.held = held
                fcntl.flock(stream, fcntl.LOCK_UN)


def worktree(sessions) -> dict[str, str]:
    """有界扫描正式工作树用于工具前后比较，不读取其他挂载或不可变快照。"""
    store = sessions.kw.get("info", {}).get("file_store")
    if store is None:
        return {}
    root = store.path("knowledge/documents")
    result: dict[str, str] = {}
    total = 0
    for path in sorted(root.rglob("*.md")):
        key = path.relative_to(store.root).as_posix()
        safe = store.path(key)
        if path.is_symlink() or not safe.is_relative_to(root):
            raise ValueError("知识工作文件不能是符号链接")
        size = safe.stat().st_size
        total += size
        if len(result) >= 1000 or size > 800000 or total > 20000000:
            raise ValueError("知识工作树超过本地同步容量，请分批处理")
        result[key] = safe.read_text(encoding="utf-8")
    return result


def published_files(sessions) -> dict[str, dict]:
    with sessions() as session:
        return {
            doc.workspace_path: {
                "document_id": doc.id,
                "version_id": version.id,
                "title": version.title,
                "body": version.body,
                "archived": version.archived,
            }
            for doc, version in session.execute(
                select(KnowledgeDocument, KnowledgeVersion).join(
                    KnowledgeVersion, KnowledgeVersion.id == KnowledgeDocument.current_version_id
                )
            )
            if doc.workspace_path
        }


def content_hash(body: str | None) -> str | None:
    return hashlib.sha256(body.encode("utf-8")).hexdigest() if body is not None else None


def publication_files(sessions, changeset_id: str) -> dict[str, str | None] | None:
    """返回草稿编辑时的工作文件指纹，供发布后比较，不把旧回执当覆盖授权。"""
    if sessions.kw.get("info", {}).get("file_store") is None:
        return {}
    with sessions() as session:
        draft = session.get(ChangeSet, changeset_id)
        if draft is None:
            return {}
        if draft.status == "published":
            # 已提交回执的重放只刷新阅读导出，不能重写或复活工作文件。
            return None
        expected = {}
        for operation in draft.operations:
            doc = session.get(KnowledgeDocument, operation["document_id"])
            binding = (draft.workspace_paths or {}).get(operation["document_id"])
            if binding:
                expected[binding["path"]] = binding["sha256"]
            else:
                path = (
                    doc.workspace_path
                    if doc and doc.workspace_path
                    else f"knowledge/documents/{operation['document_id']}.md"
                )
                base = (
                    session.get(KnowledgeVersion, operation["base_version_id"])
                    if operation.get("base_version_id")
                    else None
                )
                expected[path] = content_hash(base.body if base and not base.archived else None)
        return expected


def refresh_working_files(
    sessions, document_ids: list[str] | None = None, *, expected_files: dict | None = None
) -> list[str]:
    """发布后比较并更新工作文件；启动/重放不能覆盖人工或后续未同步的修改。

    本地编辑器不参与应用锁，因此只做内容冲突检测，不声称跨进程文件事务。
    返回保留未覆盖的路径，调用方必须将它们作为待同步状态反馈。
    """
    dirty: list[str] = []
    with sessions.begin() as session:
        store = session.info.get("file_store")
        if store is None:
            return dirty
        for doc, version in session.execute(
            select(KnowledgeDocument, KnowledgeVersion).join(
                KnowledgeVersion, KnowledgeVersion.id == KnowledgeDocument.current_version_id
            )
        ):
            if document_ids is not None and doc.id not in document_ids:
                continue
            key = doc.workspace_path or f"knowledge/documents/{doc.id}.md"
            new_mapping = doc.workspace_path is None
            doc.workspace_path = key
            target = store.path(key)
            if document_ids is None and (not new_mapping or target.exists()):
                continue
            if document_ids is not None:
                actual = (
                    content_hash(target.read_text(encoding="utf-8")) if target.exists() else None
                )
                desired = content_hash(None if version.archived else version.body)
                if actual != desired and (
                    expected_files is None or actual != expected_files.get(key)
                ):
                    dirty.append(key)
                    continue
            if version.archived:
                if document_ids is not None:
                    target.unlink(missing_ok=True)
            else:
                store.replace(key, version.body.encode())
    return dirty


def synchronize(
    context,
    paths: list[str],
    *,
    operation_key: str,
    baseline: dict | None = None,
    authority_epoch: int = 0,
) -> dict:
    """对指定文件生成一个原有变更集；版本冲突、引用与授权复用同一发布服务。"""
    from knowledgeops_agent.file_store import FileStore

    if context.workspace_root is None or context.run_id != context.root_id:
        raise PermissionError("本运行没有知识工作树写入权限")
    sessions = context.jobs.sessions
    store = FileStore(context.workspace_root)
    current = worktree(sessions)
    known = published_files(sessions) if baseline is None else baseline
    keys = sorted({path.lstrip("/") for path in paths})
    for key in keys:
        target = store.path(key)
        if not target.is_relative_to(store.path("knowledge/documents")) or target.suffix != ".md":
            raise ValueError("仅同步 knowledge/documents 下的 Markdown 文件")
    # 同一草稿上的连续编辑必须累积为修订，不能每个 edit_file 创建一个新文章。
    # 仅收集本次路径涉及的草稿；跨会话修改仍由现有 _draft_actor 权限校验。
    with sessions() as session:
        pending = [
            context.knowledge.get_draft(row.id) | {"workspace_paths": row.workspace_paths}
            for row in session.scalars(
                select(ChangeSet).where(ChangeSet.status.in_(["draft", "approved"]))
            )
            if any(binding["path"] in keys for binding in (row.workspace_paths or {}).values())
        ]
    if len(pending) > 1:
        raise ValueError("这些文件属于不同待确认草稿，请分别同步或先处理原草稿")
    previous = pending[0] if pending else None
    mapping = dict(previous["workspace_paths"]) if previous else {}
    operations_by_id = (
        {item["document_id"]: item for item in previous["operations"]} if previous else {}
    )
    for key in keys:
        target = store.path(key)
        old, body = known.get(key), current.get(key)
        pending_id = next(
            (identifier for identifier, binding in mapping.items() if binding["path"] == key), None
        )
        pending_operation = operations_by_id.pop(pending_id, None)
        if pending_id:
            mapping.pop(pending_id)
        if old and body == old["body"] and not old["archived"]:
            continue
        if body is None and (old is None or old["archived"]):
            continue
        document_id = old["document_id"] if old else pending_id or str(uuid4())
        mapping[document_id] = {"path": key, "sha256": content_hash(body)}
        base_version_id = (
            pending_operation.get("base_version_id")
            if pending_operation
            else old["version_id"]
            if old
            else None
        )
        if body is None:
            operation = {
                "action": "archive",
                "document_id": document_id,
                "base_version_id": base_version_id,
            }
        else:
            headings = [line[2:].strip() for line in body.splitlines() if line.startswith("# ")]
            operation = {
                "action": "update" if old else "create",
                "document_id": document_id,
                "title": headings[0] if headings else old["title"] if old else target.stem,
                "body": body,
            }
            if old:
                operation["base_version_id"] = base_version_id
        operations_by_id[document_id] = operation

    def guard(session):
        context.jobs.publication_guard(
            session, context.run_id, context.worker_id, authority_epoch=authority_epoch
        )

    operations = list(operations_by_id.values())
    if not operations:
        if previous:
            context.knowledge.reject(
                previous["id"],
                expected_revision=previous["revision"],
                actor_run_id=context.run_id,
                operation_key=operation_key,
                guard=guard,
            )
        return {"status": "unchanged"}
    if previous and mapping == previous["workspace_paths"]:
        # 手动补同步/工具恢复遇到相同文件，仅重试原草稿发布，不增加编辑修订。
        draft = previous
    elif previous:
        draft = context.knowledge.edit_draft(
            previous["id"],
            operations,
            expected_revision=previous["revision"],
            actor_run_id=context.run_id,
            operation_key=operation_key,
            guard=guard,
            workspace_paths=mapping,
            require_prior_read=False,
        )
    else:
        draft = context.knowledge.create_draft(
            context.root_id,
            operations,
            operation_key=operation_key,
            guard=guard,
            workspace_paths=mapping,
        )
    try:
        receipt = context.knowledge.publish(
            draft["id"], idempotency_key=operation_key, actor_run_id=context.run_id, guard=guard
        )
        return {"status": "published", **receipt}
    except PermissionError as error:
        # 文件修改保留为工作树，正式版本仍是原版本；后续聊天或 UI 可批准草稿。
        # 提交路径随草稿持久化，用户批准后仍输出到该文件，而不是新建另一份。
        return {
            "status": "draft_ready",
            "changeset_id": draft["id"],
            "message": "工作文件已修改；正式版本尚未更新检索，请先处理发布条件。",
            "reason": str(error),
        }


class KnowledgeFileSyncMiddleware(AgentMiddleware):
    """在通用写工具/命令之后自动发现本次修改，无需模型另记得调用发布工具。"""

    def wrap_tool_call(self, request, handler):
        context = request.runtime.context
        if (
            request.tool_call["name"] not in {"write_file", "edit_file", "delete", "execute"}
            or context.workspace_root is None
        ):
            return handler(request)
        with workspace_lock(context.jobs.sessions):
            before = worktree(context.jobs.sessions)
            baseline = published_files(context.jobs.sessions)
            result = handler(request)
            after = worktree(context.jobs.sessions)
            changed = [
                path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
            ]
            if not changed:
                return result
            try:
                receipt = synchronize(
                    context,
                    changed,
                    operation_key=f"files:{context.run_id}:{request.tool_call['id']}",
                    baseline=baseline,
                    authority_epoch=request.state.get("decision_authority_epoch", 0),
                )
            except (OSError, ValueError, PermissionError) as error:
                receipt = {
                    "status": "sync_failed",
                    "message": "文件操作已执行，但知识同步失败；保留工作文件，请修正后 sync_knowledge_files。",
                    "error": str(error),
                }
            context.jobs.append_event(
                context.run_id, "knowledge_files_synced", {"paths": changed, **receipt}
            )
            message = "\n知识文件同步回执：" + json.dumps(receipt, ensure_ascii=False)
            if isinstance(result, ToolMessage):
                return result.model_copy(update={"content": str(result.content) + message})
            # 上游文件工具可能携带已读文件状态，以 Command 返回；只补消息，不丢状态。
            if isinstance(getattr(result, "update", None), dict):
                result.update["messages"] = [
                    m.model_copy(update={"content": str(m.content) + message})
                    if isinstance(m, ToolMessage)
                    else m
                    for m in result.update.get("messages", [])
                ]
            return result


@tool
def sync_knowledge_files(file_paths: list[str], runtime: ToolRuntime[Any]) -> dict:
    """显式同步外部编辑或中断后未同步的知识工作文件；普通工具修改后会自动同步。

    file_paths 为 /knowledge/documents 下的 Markdown 路径，可多级目录。
    新增形成文章，修改形成新版本，删除表示归档。仍按用户的保存/预览要求处理，
    不授予新的发布许可；原引用需有效，冲突须先核对。不会同步未指定的文件。
    """
    if not 1 <= len(file_paths) <= 20:
        raise ValueError("请明确指定 1 至 20 个文件")
    with workspace_lock(runtime.context.jobs.sessions):
        return synchronize(
            runtime.context,
            file_paths,
            operation_key=f"files:{runtime.context.run_id}:{runtime.tool_call_id}",
            authority_epoch=runtime.state.get("decision_authority_epoch", 0),
        )
