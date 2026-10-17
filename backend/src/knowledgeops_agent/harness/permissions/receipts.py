"""本地命令的持久副作用回执，沿用 MCP unknown 语义而不共用其服务外键表。"""

from __future__ import annotations

import hashlib
import json

from langchain_core.messages import ToolMessage
from sqlalchemy import select

from knowledgeops_agent.app.runs.service import RunStopped
from knowledgeops_agent.persistence.models import CommandExecution, ExecutionAction, utcnow


class ExecutionUnknown(RunStopped):
    """只交由真实用户核对；模型不能自行恢复一次可能已发生的副作用。"""


def execution_fingerprint(call: dict, *, workspace: str, cwd: str) -> str:
    if not isinstance(call.get("id"), str) or not 1 <= len(call["id"]) <= 200:
        raise ValueError("执行调用需要稳定且有效的 ID")
    payload = {
        "call_id": call["id"],
        "name": call["name"],
        "args": call["args"],
        "workspace": workspace,
        "cwd": cwd,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def receipt_view(row: CommandExecution) -> dict:
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "root_id",
            "run_id",
            "tool_call_id",
            "fingerprint",
            "request",
            "status",
            "attempt",
            "revision",
            "result",
            "history",
        )
    }


class CommandJournal:
    def __init__(self, context):
        self.context = context

    def list(self) -> list[dict]:
        with self.context.jobs.sessions() as session:
            rows = session.scalars(
                select(CommandExecution)
                .where(CommandExecution.root_id == self.context.root_id)
                .order_by(CommandExecution.created_at, CommandExecution.id)
            )
            return [receipt_view(row) for row in rows]

    def start(self, call: dict, *, workspace: str, cwd: str) -> dict:
        """检查/扣额/记 running 同事务完成；在提交后才允许调用真实子进程。"""
        context = self.context
        context.jobs.guard(context.run_id, worker_id=context.worker_id)
        fingerprint = execution_fingerprint(call, workspace=workspace, cwd=cwd)
        unknown = False
        ticket: dict = {}
        with context.jobs.sessions.begin() as session:
            root = context.jobs._locked(session, context.root_id)
            row = session.scalar(
                select(CommandExecution)
                .where(
                    CommandExecution.run_id == context.run_id,
                    CommandExecution.tool_call_id == call["id"],
                )
                .with_for_update()
            )
            if row and row.fingerprint != fingerprint:
                raise PermissionError("同一命令调用的参数或工作区已变化，不能使用旧回执")
            if row and row.status in {"succeeded", "failed", "skipped"}:
                return {
                    "id": row.id,
                    "attempt": row.attempt,
                    "cached": True,
                    "result": ToolMessage.model_validate(row.result),
                }
            if row and row.status in {"running", "unknown"}:
                if row.status == "running":
                    row.status = "unknown"
                    row.revision += 1
                    row.updated_at = utcnow()
                    context.jobs._event(
                        session,
                        root,
                        "execution_unknown",
                        {"receipt_id": row.id, "run_id": row.run_id},
                    )
                unknown = True
            else:
                if root.authority_epoch != root.applied_authority_epoch:
                    raise PermissionError("用户指令已变化，不能开始旧命令")
                if row and row.authority_epoch != root.authority_epoch:
                    raise PermissionError("命令重试授权已因用户指令变化而失效")
                if row is None:
                    row = CommandExecution(
                        root_id=context.root_id,
                        run_id=context.run_id,
                        tool_call_id=call["id"],
                        fingerprint=fingerprint,
                        request={
                            "name": call["name"],
                            "args": call["args"],
                            "workspace": workspace,
                            "cwd": cwd,
                        },
                        authority_epoch=root.authority_epoch,
                        status="running",
                        attempt=1,
                        revision=1,
                    )
                    session.add(row)
                elif row.status == "retry_ready":
                    row.status = "running"
                    row.attempt += 1
                    row.revision += 1
                    row.updated_at = utcnow()
                else:
                    raise ValueError("命令回执状态不允许执行")
                context.jobs._reserve_tool(
                    session, context.run_id, "execute", call_key=f"{call['id']}:{row.attempt}"
                )
                session.flush()
                ticket = {"id": row.id, "attempt": row.attempt, "cached": False, "result": None}
        # 必须先提交 unknown 状态再抛出控制信号，不能让异常回滚这条事实。
        if unknown:
            raise ExecutionUnknown(
                "命令可能已经执行，但结果未保存。请核对原操作后选择跳过或明确重试。"
            )
        return ticket

    def finish(self, ticket: dict, message: ToolMessage) -> None:
        """即使运行刚取消，已经得到的实际结果仍是事实；旧尝试不能覆盖新尝试。"""
        with self.context.jobs.sessions.begin() as session:
            self.context.jobs._locked(session, self.context.root_id)
            row = session.scalar(
                select(CommandExecution)
                .where(CommandExecution.id == ticket["id"])
                .with_for_update()
            )
            if row is None or row.run_id != self.context.run_id or row.attempt != ticket["attempt"]:
                raise ValueError("命令执行尝试已变化")
            if row.status in {"succeeded", "failed"}:
                return
            if row.status not in {"running", "unknown"}:
                raise ValueError("命令回执已由用户处置，不能覆盖")
            row.status = "failed" if message.status == "error" else "succeeded"
            row.result = message.model_dump(mode="json")
            row.revision += 1
            row.updated_at = utcnow()

    def mark_unknown(self, ticket: dict) -> None:
        with self.context.jobs.sessions.begin() as session:
            root = self.context.jobs._locked(session, self.context.root_id)
            row = session.scalar(
                select(CommandExecution)
                .where(CommandExecution.id == ticket["id"])
                .with_for_update()
            )
            if row and row.attempt == ticket["attempt"] and row.status == "running":
                row.status = "unknown"
                row.revision += 1
                row.updated_at = utcnow()
                self.context.jobs._event(
                    session, root, "execution_unknown", {"receipt_id": row.id, "run_id": row.run_id}
                )

    def resolve(self, receipt_id: str, *, expected_revision: int, decision: str) -> dict:
        """用户入口；retry 是新的实际尝试并消耗新预算，skip 明确反馈结果未知。"""
        if decision not in {"skip", "retry"}:
            raise ValueError("未知结果只支持跳过或明确重试")
        with self.context.jobs.sessions.begin() as session:
            root = self.context.jobs._locked(session, self.context.root_id)
            row = session.scalar(
                select(CommandExecution)
                .where(CommandExecution.id == receipt_id, CommandExecution.root_id == root.id)
                .with_for_update()
            )
            if row is None:
                raise KeyError("command receipt not found")
            if row.revision != expected_revision or row.status != "unknown":
                raise ValueError("命令回执已变化，请刷新后核对")
            if root.status not in {"interrupted", "paused"} or root.cancel_requested:
                raise PermissionError("只能处理等待中的原运行，不能复活已取消任务")
            if decision == "retry" and (
                root.authority_epoch != row.authority_epoch
                or root.authority_epoch != root.applied_authority_epoch
            ):
                raise PermissionError("用户指令已变化，旧命令不能重试")
            row.history = [
                *row.history,
                {
                    "attempt": row.attempt,
                    "actor": "user",
                    "decision": decision,
                    "at": utcnow().isoformat(),
                    "previous_revision": row.revision,
                },
            ]
            row.status = "retry_ready" if decision == "retry" else "skipped"
            if decision == "retry":
                # 用户此刻明确重试一次，属于新的逐次许可。它不恢复被取消的
                # 整批授权或命令规则；实际执行仍复核工具、资源和最新 deny。
                action = session.scalar(
                    select(ExecutionAction)
                    .where(
                        ExecutionAction.run_id == row.run_id,
                        ExecutionAction.tool_call_id == row.tool_call_id,
                    )
                    .with_for_update()
                )
                if action is not None:
                    row.history = [
                        *row.history[:-1],
                        {**row.history[-1], "previous_approval": action.decision},
                    ]
                    action.status = "approved"
                    action.decision = {
                        "actor": "user",
                        "approve": True,
                        "scope": "once",
                        "match": "exact",
                        "retry_receipt_id": row.id,
                        "proposed_under": action.decision.get("proposed_under", {}),
                    }
                    action.resolved_at = utcnow()
            if decision == "skip":
                row.result = ToolMessage(
                    "用户核对后选择跳过此命令。原执行结果未知，不能声称命令成功或没有副作用。",
                    name="execute",
                    tool_call_id=row.tool_call_id,
                    status="error",
                ).model_dump(mode="json")
            row.revision += 1
            row.updated_at = utcnow()
            self.context.jobs._event(
                session, root, "execution_reconciled", {"receipt_id": row.id, "decision": decision}
            )
            return receipt_view(row)
