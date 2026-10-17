"""真人文件核对入口：只检查或跳过原操作，不把一次点击变成新的文件写入。"""

from pathlib import Path

from langchain_core.messages import ToolMessage
from sqlalchemy import select

from knowledgeops_agent.harness.sandbox.files import _row_view, file_hash, path_lock
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.persistence.models import FileOperation, utcnow

PENDING_FILE_STATES = ("unknown", "conflict")


class FileReconciliation:
    def __init__(self, context):
        self.context = context

    def _path(self, row: dict) -> Path:
        root = self.context.workspace_root
        if root is None or str(Path(root).resolve()) != row["workspace"]:
            raise ValueError("原工作区当前不可用；请恢复挂载后核对，或保留现状跳过")
        # 保留字面路径检查符号链接，不能先 resolve 后将链接伪装成普通文件。
        store = FileStore(Path(root))
        target = Path(root) / row["path"]
        if target.is_symlink() or store.path(row["path"]) != target.absolute():
            raise ValueError("文件路径包含符号链接，不能核对为原目标")
        return target

    def list(self) -> list[dict]:
        with self.context.jobs.sessions() as session:
            rows = [_row_view(row) for row in session.scalars(select(FileOperation).where(
                FileOperation.root_id == self.context.root_id,
                FileOperation.status.in_(PENDING_FILE_STATES),
            ).order_by(FileOperation.created_at, FileOperation.id))]
        for row in rows:
            try:
                target = self._path(row)
                row["current_hash"] = file_hash(target)
                row["matches_expected"] = row["current_hash"] == row["after_hash"]
                row["current_preview"] = ""
                if target.is_file():
                    with target.open("rb") as stream:
                        row["current_preview"] = stream.read(4000).decode("utf-8", errors="replace")
            except (OSError, ValueError) as exc:
                row["inspection_error"] = str(exc)
                row["matches_expected"] = False
            # 不向界面暴露内部不可变文件键或知识同步实现记录。
            row.pop("content_key", None)
            row.pop("result", None)
        return rows

    def resolve(self, receipt_id: str, *, expected_revision: int, decision: str) -> dict:
        if decision not in {"skip", "verify"}:
            raise ValueError("文件结果只能保留现状跳过或重新核对，不能盲目重试")
        with self.context.jobs.sessions.begin() as session:
            root = self.context.jobs._locked(session, self.context.root_id)
            row = session.scalar(select(FileOperation).where(
                FileOperation.id == receipt_id, FileOperation.root_id == root.id,
            ).with_for_update())
            if row is None:
                raise KeyError("file receipt not found")
            if row.revision != expected_revision or row.status not in PENDING_FILE_STATES:
                raise ValueError("文件核对状态已变化，请刷新")
            if root.status not in {"interrupted", "paused"} or root.cancel_requested:
                raise PermissionError("只能处理等待中的原任务，不能复活取消的操作")
            result = row.result or {}
            if decision == "verify":
                if root.authority_epoch != row.authority_epoch or root.authority_epoch != root.applied_authority_epoch:
                    raise PermissionError("原要求已变化；请跳过旧操作并重新读取")
                target = self._path(_row_view(row))
                with path_lock(row.workspace, target.as_posix()):
                    if file_hash(target) != row.after_hash:
                        raise ValueError("当前文件仍不等于预期结果；保留现状，不覆盖文件")
                    # 下一次原调用只补齐回执/知识同步，mutate 会再次比较哈希。
                    row.status = "applied"
            else:
                row.status = "skipped"
                result = {**result, "message": ToolMessage(
                    content="用户已核对并选择保留文件现状，跳过原操作；其成功结果未确认。若仍需修改，先重新读取当前文件，再提出新的操作。",
                    name=row.operation, tool_call_id=row.tool_call_id, status="error",
                ).model_dump(mode="json")}
            row.result = {**result, "user_decision": {
                "decision": decision, "at": utcnow().isoformat(),
                "previous_revision": row.revision, "actor": "user",
            }}
            row.revision += 1
            row.updated_at = utcnow()
            self.context.jobs._event(session, root, "file_operation_resolved", {
                "receipt_id": row.id, "run_id": row.run_id, "decision": decision,
            })
            return {"id": row.id, "status": row.status, "revision": row.revision}
