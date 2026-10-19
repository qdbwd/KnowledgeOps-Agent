"""真人补问的持久记录与等待接续；回答提供信息，不授予执行或发布权限。

图只保存中断位置，PG 保存问题和答案。HTTP 回答与 worker 落下等待状态可以
先后交错，双方都尝试同一受锁保护的 outbox 接续，不依赖浏览器保持连接。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from knowledgeops_agent.persistence.models import (
    ExecutionAction,
    MCPCall,
    MCPContinuation,
    MessageDelivery,
    Run,
    RunControl,
    RunDispatch,
    RunQuestion,
)


class InputWaiting(Exception):
    """检查点已保留，释放 worker 槽位等待真人。"""


class QuestionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=4000)
    options: list[str] = Field(default_factory=list, max_length=8)
    context: str = Field(default="", max_length=2000)


class QuestionAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_revision: int = Field(ge=1)
    kind: Literal["text", "option", "decline", "mcp"]
    value: str | dict = Field(default="", max_length=12000)


def question_view(row: RunQuestion) -> dict:
    return {"id": row.id, "run_id": row.run_id, "root_id": row.root_id,
            "kind": row.kind, "revision": row.revision, "request": row.request,
            "answer": row.answer, "status": row.status}


def invalidate_inputs(session, *, root_id: str | None = None, run_id: str | None = None,
                      cancelled: bool = False) -> None:
    """撤下指定范围的旧补问/续接；已在网络中的请求仍由原回执记录真实结果。"""
    if (root_id is None) == (run_id is None):
        raise ValueError("输入撤回须指定一个根任务或实际运行")
    question_scope = RunQuestion.run_id == run_id if run_id else RunQuestion.root_id == root_id
    for row in session.scalars(select(RunQuestion).where(question_scope, RunQuestion.status == "pending")):
        row.status = "cancelled" if cancelled else "stale"
        row.revision += 1
    call_scope = MCPCall.run_id == run_id if run_id else MCPCall.run_id.in_(
        select(Run.id).where(Run.root_id == root_id)
    )
    for row in session.scalars(select(MCPCall).where(
        call_scope, MCPCall.status.in_(["pending", "reviewing", "approved", "waiting_input"]),
    )):
        row.status = "denied"
        row.decision = {**(row.decision or {}), "actor": "application", "approve": False,
                        "invalidated_reason": "用户取消运行" if cancelled else "任务要求变化，旧提议已失效"}
        for continuation in session.scalars(select(MCPContinuation).where(
            MCPContinuation.mcp_call_id == row.id, MCPContinuation.status.in_(["waiting", "ready"]),
        )):
            continuation.status = "cancelled" if cancelled else "invalidated"


def queue_after_answers(jobs, root_id: str) -> dict | None:
    """接续已获回答的实际运行；主子分别等自己的问题，根暂停/异常重启仍优先。"""
    with jobs.sessions.begin() as session:
        root = jobs._locked(session, root_id)
        control = session.get(RunControl, root.id)
        if root.parent_id or root.status not in {"running", "queued", "waiting_input", "waiting_children"} or root.cancel_requested or (
            control and control.pause_requested
        ):
            return None
        resumed = None
        for run in session.scalars(select(Run).where(
            Run.root_id == root.id, Run.status == "waiting_input",
        ).order_by(Run.id).with_for_update()):
            if run.id != root.id and root.authority_epoch != root.applied_authority_epoch:
                # 主任务先解释真人引导，再接回因旧问题撤回而等待的子任务。
                continue
            own_control = session.get(RunControl, run.id)
            if run.cancel_requested or (own_control and own_control.pause_requested):
                continue
            if session.scalar(select(RunQuestion.id).where(
                RunQuestion.run_id == run.id, RunQuestion.status == "pending",
            ).limit(1)) or session.scalar(select(ExecutionAction.id).where(
                ExecutionAction.run_id == run.id, ExecutionAction.status.in_(["pending", "reviewing"]),
            ).limit(1)) or session.scalar(select(MCPCall.id).where(
                MCPCall.run_id == run.id, MCPCall.status.in_(["pending", "reviewing"]),
            ).limit(1)):
                continue
            run.generation += 1
            if run.id == root.id:
                for delivery in session.scalars(select(MessageDelivery).where(
                    MessageDelivery.target_root_id == root.id, MessageDelivery.status == "pending",
                )):
                    delivery.target_generation = run.generation
            run.status, run.worker_id, run.stop_reason = "queued", None, None
            session.add(RunDispatch(run_id=run.id, generation=run.generation, next_attempt_at=jobs.now()))
            jobs._event(session, run, "input_ready", {"status": "queued", "generation": run.generation})
            resumed = jobs._view(run)
        return resumed


class RunQuestions:
    def __init__(self, jobs):
        self.jobs = jobs

    def list(self, root_id: str) -> list[dict]:
        with self.jobs.sessions() as session:
            return [question_view(row) for row in session.scalars(select(RunQuestion).where(
                RunQuestion.root_id == root_id,
            ).order_by(RunQuestion.created_at, RunQuestion.id))]

    def ensure(self, context, tool_call_id: str, request: dict, *, kind: str = "clarification") -> dict:
        """同一工具提议重入时复用问题，参数不允许在原身份下漂移。"""
        body = QuestionInput.model_validate(request).model_dump()
        if any(not item.strip() or len(item) > 500 for item in body["options"]):
            raise ValueError("选项须为非空且不超过500字符的文字")
        if len(set(body["options"])) != len(body["options"]):
            raise ValueError("问题选项不能重复")
        with self.jobs.sessions.begin() as session:
            root, executor = self.jobs.execution_guard(session, context.run_id, context.worker_id)
            row = session.scalar(select(RunQuestion).where(
                RunQuestion.run_id == context.run_id, RunQuestion.tool_call_id == tool_call_id,
                RunQuestion.round == 0,
            ))
            if row:
                if row.request != body or row.kind != kind:
                    raise ValueError("原问题身份不能替换为另一份内容")
                return question_view(row)
            row = RunQuestion(root_id=root.id, run_id=context.run_id, tool_call_id=tool_call_id,
                              kind=kind, authority_epoch=executor.authority_epoch, request=body)
            session.add(row)
            session.flush()
            control = session.get(RunControl, root.id)
            if control is None:
                control = RunControl(run_id=root.id, wait_spec={})
                session.add(control)
            control.wait_spec = {**control.wait_spec, "question_ids": [
                *control.wait_spec.get("question_ids", []), row.id,
            ]}
            session.flush()
            self.jobs._event(session, root, "question_pending", {"question_id": row.id})
            return question_view(row)

    def record_interrupts(self, context, snapshot) -> bool:
        found = False
        with self.jobs.sessions.begin() as session:
            self.jobs._locked(session, context.root_id)
            for item in snapshot.interrupts:
                if not isinstance(item.value, dict) or "run_question_id" not in item.value:
                    continue
                row = session.get(RunQuestion, item.value["run_question_id"])
                if not row or row.run_id != context.run_id:
                    raise ValueError("补问中断不属于当前运行")
                row.interrupt_id = item.id
                found = True
        return found

    def resume_answers(self, context, snapshot) -> dict:
        self.record_interrupts(context, snapshot)
        answers = {}
        with self.jobs.sessions() as session:
            for item in snapshot.interrupts:
                if not isinstance(item.value, dict) or "run_question_id" not in item.value:
                    continue
                row = session.get(RunQuestion, item.value["run_question_id"])
                if row.status == "pending":
                    raise InputWaiting("问题仍在等待你的回答")
                answers[item.id] = {"status": row.status, "answer": row.answer}
        return answers

    def result(self, question_id: str, root_id: str) -> dict:
        with self.jobs.sessions() as session:
            row = session.get(RunQuestion, question_id)
            if not row or row.root_id != root_id:
                raise ValueError("问题不属于当前任务")
            executor = session.get(Run, row.run_id)
            if row.authority_epoch != executor.authority_epoch or row.status in {"stale", "cancelled"}:
                return {"status": "stale", "answer": None, "instruction": "原问题已失效，请结合最新要求重新判断。"}
            if row.status == "pending":
                raise InputWaiting("问题尚未回答")
            return {"status": row.status, "answer": row.answer, "question_id": row.id,
                    "origin": "human", "instruction": "这是信息回答，不是执行或知识发布授权。"}

    def answer(self, root_id: str, question_id: str, answer: QuestionAnswer) -> dict:
        wanted = {"kind": answer.kind, "value": "" if answer.kind == "decline" else answer.value}
        with self.jobs.sessions.begin() as session:
            root = self.jobs._locked(session, root_id)
            row = session.get(RunQuestion, question_id)
            if not row or row.root_id != root.id:
                raise KeyError("question not found")
            executor = session.get(Run, row.run_id)
            if row.status in {"answered", "declined"} and row.answer == wanted:
                result = question_view(row)
            else:
                if row.revision != answer.expected_revision or row.status != "pending":
                    raise ValueError("问题已变化或已处理，请刷新后查看")
                if (root.cancel_requested or executor.cancel_requested
                    or root.status not in {"running", "queued", "waiting_children", "waiting_input", "paused", "interrupted"}
                    or executor.status not in {"running", "waiting_input", "paused", "interrupted"}):
                    raise PermissionError("原任务已结束，不能通过回答复活")
                if row.authority_epoch != executor.authority_epoch:
                    raise ValueError("用户要求已变化，旧问题失效")
                if row.kind == "mcp":
                    from knowledgeops_agent.harness.mcp.input import validate_answer

                    validate_answer(row.request, answer.kind, answer.value)
                else:
                    if answer.kind == "mcp" or not isinstance(answer.value, str):
                        raise ValueError("普通问题只接受文字、选项或明确拒绝")
                    if answer.kind != "decline" and not answer.value:
                        raise ValueError("请填写回答或明确选择不回答")
                    if answer.kind == "option" and answer.value not in row.request.get("options", []):
                        raise ValueError("所选内容不在原问题的选项中")
                row.answer, row.status = wanted, "declined" if answer.kind == "decline" else "answered"
                row.revision += 1
                row.updated_at = self.jobs.now()
                self.jobs._event(session, root, "question_answered", {"question_id": row.id, "status": row.status})
                result = question_view(row)
        queue_after_answers(self.jobs, root_id)
        return result

    def settle_wait(self, context, outcome: dict) -> dict:
        cancelled = False
        with self.jobs.sessions.begin() as session:
            root = self.jobs._locked(session, context.run_id)
            if root.worker_id != context.worker_id:
                raise PermissionError("worker owner does not match")
            if root.cancel_requested:
                cancelled = True
            else:
                from knowledgeops_agent.learning.budget import settle_time

                settle_time(session, root)
                control = session.get(RunControl, root.id)
                paused = bool(control and control.pause_requested)
                root.status = "paused" if paused else "waiting_input"
                root.stop_reason = "等待你的回答；已暂停任务仍需明确继续" if paused else "等待你的回答，执行资源已释放"
                root.outcome, root.worker_id = {**outcome, "kind": "waiting_input"}, None
                from knowledgeops_agent.app.runs.children import sync_group_clock

                sync_group_clock(self.jobs, session, session.get(Run, root.root_id))
                self.jobs._event(session, root, "waiting_input", {"status": root.status})
        if cancelled:
            return self.jobs.finish(context.run_id, context.worker_id, status="cancelled", outcome=outcome)
        queue_after_answers(self.jobs, context.root_id)
        # 回答可能同时唤醒同组的另一个运行；worker 必须返回自己这一项的状态。
        return self.jobs.get_run(context.run_id)
