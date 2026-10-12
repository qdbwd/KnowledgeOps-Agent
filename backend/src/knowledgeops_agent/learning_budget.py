"""知识单元累计预算：先预占再调用，跨Run、子Agent及恢复不清零。

模型账本仍是请求/重放的事实来源。本表只保存同一单元的控制计数；两个数据库
之间采取保守预占：崩溃可能多占一次，绝不因不确定结果再免费调用一次。
"""

from sqlalchemy import select

from knowledgeops_agent.db import Conversation, LearningTask, LearningUnit, OperationReceipt, Run
from knowledgeops_agent.run_resources import content_hash


def active_unit(session, root: Run, active_ms: int, *, lock=False):
    from knowledgeops_agent.jobs import RunStopped

    if root.learning_unit_id is None:
        return None
    query = select(LearningUnit).where(LearningUnit.id == root.learning_unit_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    unit = session.scalar(query)
    task = session.get(LearningTask, root.learning_task_id)
    conversation = session.get(Conversation, root.conversation_id)
    if (
        unit is None
        or task is None
        or unit.task_id != task.id
        or task.status != "active"
        or task.confirmed_requirements_revision != task.requirements_revision
        or conversation.selected_learning_task_id != task.id
    ):
        raise RunStopped("学习任务已暂停、撤回、切换或等待确认")
    if (
        unit.active_ms + max(0, active_ms - root.learning_accounted_ms)
        >= unit.budgets["active_seconds"] * 1000
    ):
        raise RunStopped("learning unit active time budget exhausted")
    return unit


def settle_time(session, root: Run):
    """只统计根运行的活跃墙钟时间，子运行与主运行重叠的时间不重复相加。"""
    if root.parent_id is None and root.learning_unit_id:
        unit = session.scalar(
            select(LearningUnit).where(LearningUnit.id == root.learning_unit_id).with_for_update()
        )
        unit.active_ms += max(0, root.active_ms - root.learning_accounted_ms)
        root.learning_accounted_ms = root.active_ms


def reserve_model(jobs, reservation: str, root_id: str, run_id: str):
    """供应商访问前由网关调用，reservation来自可信账本而非模型输出。"""
    from knowledgeops_agent.model_gateway import ModelBudgetExceeded

    with jobs.sessions.begin() as session:
        root = jobs._locked(session, root_id)
        jobs._check(root)
        unit = active_unit(session, root, jobs._active_ms(root), lock=True)
        if unit is None:
            return
        key = "learning-model:" + reservation
        digest = content_hash({"unit": unit.id, "run": run_id})
        prior = session.get(OperationReceipt, key)
        if prior:
            if prior.request_hash != digest:
                raise ValueError("模型预算回执身份不一致")
            return
        if unit.model_calls >= unit.budgets["model_calls"]:
            raise ModelBudgetExceeded("learning unit model request budget exhausted")
        unit.model_calls += 1
        session.add(
            OperationReceipt(operation_key=key, request_hash=digest, result={"unit_id": unit.id})
        )
