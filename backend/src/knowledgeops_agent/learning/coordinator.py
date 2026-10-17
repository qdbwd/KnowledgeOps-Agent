"""有限学习任务的持久推进，不用额外监督模型，不伪造真人消息。

安排运行、推进回执和队列外盒在同一业务事务提交；下一步只从已发布版本、
草稿修订及真实协作记录推导。普通最终文字不构成进度，也不能自授发布权限。
"""

import json

from sqlalchemy import select

from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.harness.runtime.resources import content_hash
from knowledgeops_agent.persistence.models import (
    ChangeSet,
    Conversation,
    LearningAdvance,
    LearningCollaboration,
    LearningTask,
    LearningUnit,
    Run,
    RunDispatch,
    new_id,
    utcnow,
)

OPEN = ("queued", "running", "paused", "waiting_input", "waiting_children")
ELIGIBLE = ("pending", "drafting", "reading", "reviewing", "ready")


def progress_hash(session, unit):
    draft = session.get(ChangeSet, unit.draft_id) if unit.draft_id else None
    reports = list(
        session.execute(
            select(LearningCollaboration.id, LearningCollaboration.status)
            .where(LearningCollaboration.unit_id == unit.id)
            .order_by(LearningCollaboration.id)
        )
    )
    return content_hash(
        {
            "draft": [draft.id, draft.revision, draft.status] if draft else None,
            "reports": [list(r) for r in reports],
            "result": unit.result_version_id,
        }
    )


def advance(session, conversation_id: str):
    """调用者事务内调度至多一个单元；会话行锁防止并发完成回调重复续跑。"""
    lock_control(session)
    session.flush()
    conversation = session.scalar(
        select(Conversation).where(Conversation.id == conversation_id).with_for_update()
    )
    if not conversation or conversation.deletion_state != "active" or not conversation.selected_learning_task_id:
        return None
    task = session.scalar(
        select(LearningTask)
        .where(LearningTask.id == conversation.selected_learning_task_id)
        .with_for_update()
    )
    if (
        task.mode != "delegated"
        or task.status != "active"
        or task.confirmed_requirements_revision != task.requirements_revision
    ):
        return None
    # 等待审批也是原任务的一部分，不能创建下一根运行绕过批准或重置预算。
    if session.scalar(
        select(Run.id)
        .where(
            Run.conversation_id == conversation.id,
            Run.parent_id.is_(None),
            Run.status == "interrupted",
            Run.outcome["kind"].astext.in_(["waiting_approval", "execution_unknown"]),
        )
        .limit(1)
    ):
        return None
    # 真人排队先执行；已安排的自动运行也不重复创建。领取时再检查一次优先级。
    if session.scalar(
        select(Run.id)
        .where(
            Run.conversation_id == conversation.id, Run.parent_id.is_(None), Run.status.in_(OPEN)
        )
        .limit(1)
    ):
        return None
    units = list(
        session.scalars(
            select(LearningUnit)
            .where(LearningUnit.task_id == task.id)
            .order_by(LearningUnit.position)
            .with_for_update()
        )
    )
    by_key = {u.key: u for u in units}
    for unit in units:
        if unit.status not in ELIGIBLE:
            continue
        if (
            unit.model_calls >= unit.budgets["model_calls"]
            or unit.tool_calls >= unit.budgets["tool_calls"]
            or unit.active_ms >= unit.budgets["active_seconds"] * 1000
        ):
            unit.status, unit.stop_reason = (
                "blocked",
                "知识单元累计预算已耗尽，保留成果，不能换Run重置",
            )
            continue
        if any(
            by_key[key].status in {"blocked", "cancelled", "dependency_blocked"}
            for key in unit.dependencies
        ):
            unit.status, unit.stop_reason = (
                "dependency_blocked",
                "前置单元未完成，不能自动跳过其要求",
            )
            continue
        if any(by_key[key].status != "published" for key in unit.dependencies):
            continue
        task.advance_sequence += 1
        identifier = new_id()
        description = {
            "task_id": task.id,
            "unit_id": unit.id,
            "title": unit.title,
            "goal": unit.goal,
            "draft_id": unit.draft_id,
            "requirements_revision": task.requirements_revision,
        }
        from knowledgeops_agent.harness.permissions.settings import runtime_inputs

        run = Run(
            id=identifier,
            root_id=identifier,
            checkpoint_id=identifier,
            conversation_id=conversation.id,
            learning_task_id=task.id,
            learning_unit_id=unit.id,
            origin="automatic",
            request_key=f"learning:{task.id}:{task.advance_sequence}",
            task="应用协调器安排的学习单元（不是新真人消息或额外授权）：\n"
            + json.dumps(description, ensure_ascii=False),
            input_data={
                **runtime_inputs(session, conversation),
                "publication_mode": "ask",
                "knowledge_only": False,
                "delegation": True,
                "auxiliary": False,
                "source_ids": [],
                "learning_requirements_revision": task.requirements_revision,
            },
            budgets={
                "model_requests": unit.budgets["model_calls"],
                **{k: v for k, v in unit.budgets.items() if k != "model_calls"},
            },
        )
        session.add(run)
        session.flush()
        session.add(
            LearningAdvance(
                task_id=task.id,
                unit_id=unit.id,
                run_id=identifier,
                sequence=task.advance_sequence,
                requirements_revision=task.requirements_revision,
                before_state=progress_hash(session, unit),
            )
        )
        session.add(RunDispatch(run_id=identifier, generation=1, next_attempt_at=utcnow()))
        if unit.status == "pending":
            unit.status = "drafting"
        unit.stop_reason = None
        return identifier
    if units and all(u.status == "published" for u in units):
        task.status, task.stop_reason = "completed", None
    elif any(u.status in {"waiting", "awaiting_approval"} for u in units):
        task.status, task.stop_reason = "waiting", "已有草稿或待决事项，需要用户处理后继续"
    else:
        task.status, task.stop_reason = (
            "partial",
            "没有可自动推进的单元，请查看保留的成果和受阻原因",
        )
    return None


def finished(session, run):
    """与根运行终态提交；失败不会偷偷新建Run绕过手动恢复。"""
    if run.parent_id or not run.conversation_id or run.kind == "context_compaction":
        return
    if run.outcome.get("kind") in {"waiting_approval", "execution_unknown"}:
        # 保留任务的 active 配置，等待用户后续决定；advance 上方会阻止另开 Run。
        return
    record = session.scalar(
        select(LearningAdvance).where(LearningAdvance.run_id == run.id).with_for_update()
    )
    if record:
        record.status, record.reason = run.status, run.stop_reason
        unit = session.get(LearningUnit, record.unit_id)
        task = session.get(LearningTask, record.task_id)
        if run.status != "completed":
            if task.status == "active":
                task.status, task.stop_reason = (
                    "waiting",
                    run.stop_reason or "执行中断，等待用户手动恢复",
                )
            return
        if unit.status not in {"published", "blocked", "waiting", "awaiting_approval"}:
            if record.requirements_revision != task.requirements_revision:
                unit.status, unit.stop_reason = "drafting", "用户更新了要求，下一运行按新修订继续"
            elif unit.status == "ready" and not task.auto_publish:
                unit.status, unit.stop_reason = "awaiting_approval", "草稿已审读，等待用户确认保存"
            elif record.before_state == progress_hash(session, unit):
                unit.status, unit.stop_reason = (
                    "blocked",
                    "本次执行没有产生新的草稿修订或协作结果，已停止空转",
                )
    advance(session, run.conversation_id)


def stop_runs(session, task, *, cancel=False, skip_run_id=None):
    """暂停队列与后续动作。运行中的进程由下一次安全点守卫停止，不杀数据库事务。"""
    from knowledgeops_agent.app.runs.service import RunService

    for run in session.scalars(
        select(Run)
        .where(
            Run.learning_task_id == task.id,
            Run.parent_id.is_(None),
            Run.learning_unit_id.is_not(None),
            Run.status.in_(OPEN),
        )
        .with_for_update()
    ):
        if run.id == skip_run_id:
            continue
        if run.status == "queued":
            run.status = "cancelled" if cancel else "interrupted"
            run.stop_reason = task.stop_reason
            if cancel:
                run.finished_at = utcnow()
                record = session.scalar(select(LearningAdvance).where(LearningAdvance.run_id == run.id))
                if record is not None:
                    record.status, record.reason = "cancelled", run.stop_reason
        elif cancel:
            run.cancel_requested = True
        RunService(None)._event(
            session,
            run,
            "learning_control",
            {"task_status": task.status, "reason": task.stop_reason},
        )


def resume_task(session, task):
    """用户明确恢复时复用未结束Run，不创建新预算；若已完成则推进下一步。"""
    from knowledgeops_agent.app.runs.service import RunService
    from knowledgeops_agent.learning.intent import BoundSessions

    for unit in session.scalars(
        select(LearningUnit).where(
            LearningUnit.task_id == task.id, LearningUnit.status == "waiting"
        )
    ):
        unit.status, unit.stop_reason = "drafting", None

    latest = session.scalar(
        select(Run)
        .where(
            Run.learning_task_id == task.id,
            Run.parent_id.is_(None),
            Run.learning_unit_id.is_not(None),
        )
        .order_by(Run.created_at.desc())
        .limit(1)
    )
    if latest and latest.status in {"interrupted", "failed", "cancelled", "budget_exhausted"}:
        unit = session.get(LearningUnit, latest.learning_unit_id)
        from knowledgeops_agent.learning.collaboration import _frozen_learning

        frozen = _frozen_learning(session, latest)
        if unit.status != "published":
            if frozen and frozen["task"]["requirements_revision"] != task.requirements_revision:
                # 新要求不能偷换旧检查点依据。显式恢复后另起Run，保留旧成果和单元预算。
                latest.stop_reason = "任务要求已变化；保留原运行，按新要求继续同一知识单元"
                unit.status, unit.stop_reason = "drafting", None
            else:
                RunService(BoundSessions(session)).resume(latest.id)
                return
    advance(session, task.conversation_id)


def view_advances(sessions, task_id):
    with sessions() as session:
        return [
            {
                "id": r.id,
                "unit_id": r.unit_id,
                "run_id": r.run_id,
                "sequence": r.sequence,
                "status": r.status,
                "reason": r.reason,
            }
            for r in session.scalars(
                select(LearningAdvance)
                .where(LearningAdvance.task_id == task_id)
                .order_by(LearningAdvance.sequence)
            )
        ]
