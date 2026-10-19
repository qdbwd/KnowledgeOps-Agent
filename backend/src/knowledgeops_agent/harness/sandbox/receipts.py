"""未启动文件操作的持久撤回；控制入口无需导入工具执行器或构造Agent。"""

from sqlalchemy import select

from knowledgeops_agent.persistence.models import FileOperation, utcnow


def skip_prepared_file(row: FileOperation, reason: str) -> None:
    """prepared尚未接触目标，可确定记为未执行；不能用于running或unknown。"""
    if row.status != "prepared":
        raise ValueError("只能直接撤回未启动的文件操作")
    row.status = "skipped"
    # 存最小ToolMessage字段，重放时由原ToolMessage.model_validate补齐默认值。
    row.result = {**(row.result or {}), "not_executed": reason, "message": {
        "type": "tool", "content": "未执行：" + reason, "name": row.operation,
        "tool_call_id": row.tool_call_id, "status": "error",
    }}
    row.revision += 1
    row.updated_at = utcnow()


def invalidate_prepared_files(session, *, reason: str, root_id=None, run_id=None) -> None:
    """取消/引导持有运行控制锁时结束未启动回执；没有worker也不会残留准备中。

    暂停仍保留准备内容供恢复。已登记启动的操作结果不由取消猜测或覆盖。
    """
    if (root_id is None) == (run_id is None):
        raise ValueError("必须指定一个文件回执撤回范围")
    scope = FileOperation.root_id == root_id if root_id is not None else FileOperation.run_id == run_id
    for row in session.scalars(select(FileOperation).where(
        scope, FileOperation.status == "prepared",
    ).order_by(FileOperation.id).with_for_update()):
        skip_prepared_file(row, reason)
