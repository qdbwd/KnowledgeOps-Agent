"""把 LangChain HITL 的具体中断接到应用审批、PG 事务和既有调度。

中断与继续使用框架公开接口；本模块持有真实用户决定和固定动作身份，
不让模型修改授权，也不复制一套工具执行图。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.messages import AIMessage
from langgraph.types import Command, interrupt
from sqlalchemy import select

from knowledgeops_agent.app.runs.service import RunStopped
from knowledgeops_agent.harness.permissions.policy import evaluate_execution
from knowledgeops_agent.harness.permissions.receipts import execution_fingerprint
from knowledgeops_agent.harness.permissions.rules import reusable_command, validate_rule
from knowledgeops_agent.harness.permissions.settings import ExecutionSettings
from knowledgeops_agent.persistence.models import (
    CommandExecution,
    ExecutionAction,
    ExecutionRule,
    FileOperation,
    MCPCall,
    MessageDelivery,
    Run,
    RunControl,
    RunDispatch,
    RunQuestion,
    utcnow,
)


class ApprovalWaiting(RunStopped):
    """应用运行已持久等待真人；主图和子图都不能把它交付成成功结果。"""


def batch_identity(state: dict) -> tuple[str, list[str]]:
    message = next(
        (
            item
            for item in reversed(state.get("messages", []))
            if isinstance(item, AIMessage) and item.tool_calls
        ),
        None,
    )
    ids: list[str] = []
    for call in message.tool_calls if message else []:
        call_id = call.get("id")
        if not call_id:
            raise ValueError("工具提议缺少持久调用 ID，不能建立审批批次")
        ids.append(call_id)
    return hashlib.sha256(json.dumps(ids).encode()).hexdigest(), ids


def workspace_paths(context) -> tuple[str, str]:
    root = (context.resources or {}).get("workspace", {}).get("root") or context.workspace_root
    if root is None:
        raise PermissionError("当前运行没有命令工作区")
    path = str(Path(root).resolve())
    return path, path


def action_view(row: ExecutionAction) -> dict:
    argv = reusable_command(row.arguments.get("command", ""))
    return {
        "id": row.id,
        "root_id": row.root_id,
        "run_id": row.run_id,
        "tool_call_id": row.tool_call_id,
        "tool_name": row.tool_name,
        "arguments": row.arguments,
        "fingerprint": row.fingerprint,
        "workspace": row.workspace,
        "cwd": row.cwd,
        "status": row.status,
        "batch_key": row.batch_key,
        "position": row.position,
        "decision": row.decision,
        "rule_candidate": {"argv": argv, "match": "exact", "cwd": row.cwd} if argv else None,
    }


def queue_after_decisions(jobs, root_id: str) -> dict | None:
    """全批决定就绪后只投递一次，不走普通 resume 的授权失效/重置流程。"""
    requested_id = root_id
    root_id = jobs.get_run(requested_id)["root_id"]
    with jobs.sessions.begin() as session:
        root = jobs._locked(session, root_id)
        control = session.get(RunControl, root.id)
        child_result = None
        # 异步子任务只等待自己的决定；根暂停、异常重启及未应用引导不授予继续权。
        if (root.status in {"running", "queued", "waiting_children", "waiting_input"}
                and not root.cancel_requested and not (control and control.pause_requested)
                and root.authority_epoch == root.applied_authority_epoch):
            for child in session.scalars(select(Run).where(
                Run.parent_id == root.id, Run.status == "interrupted",
                Run.outcome["kind"].astext == "waiting_approval",
            ).order_by(Run.id).with_for_update()):
                own_control = session.get(RunControl, child.id)
                if child.cancel_requested or (own_control and own_control.pause_requested):
                    continue
                blocked = any(session.scalar(select(model.id).where(
                    model.run_id == child.id, model.status.in_(statuses),
                ).limit(1)) for model, statuses in (
                    (ExecutionAction, ["pending"]), (MCPCall, ["pending", "running", "waiting_input"]),
                    (CommandExecution, ["running", "unknown"]), (FileOperation, ["unknown", "conflict"]),
                    (RunQuestion, ["pending"]),
                ))
                if blocked:
                    continue
                child.generation += 1
                child.status, child.worker_id, child.stop_reason, child.finished_at = "queued", None, None, None
                session.add(RunDispatch(run_id=child.id, generation=child.generation, next_attempt_at=jobs.now()))
                jobs._event(session, child, "approval_resumed", {"status": "queued", "generation": child.generation})
                if child.id == requested_id:
                    child_result = jobs._view(child)
        if (
            root.parent_id
            or root.status != "interrupted"
            or root.cancel_requested
            or root.outcome.get("kind") not in {"waiting_approval", "execution_unknown"}
        ):
            return child_result
        if session.scalar(
            select(ExecutionAction.id)
            .where(ExecutionAction.root_id == root.id, ExecutionAction.status == "pending")
            .limit(1)
        ):
            return None
        if session.scalar(
            select(CommandExecution.id)
            .where(
                CommandExecution.root_id == root.id,
                CommandExecution.status.in_(["running", "unknown"]),
            )
            .limit(1)
        ):
            return None
        if session.scalar(
            select(FileOperation.id).where(
                FileOperation.root_id == root.id,
                FileOperation.status.in_(["unknown", "conflict"]),
            ).limit(1)
        ):
            return None
        if session.scalar(
            select(MCPCall.id)
            .join(Run, Run.id == MCPCall.run_id)
            .where(Run.root_id == root.id, MCPCall.status.in_(["pending", "running"]))
            .limit(1)
        ):
            return None
        root.generation += 1
        for delivery in session.scalars(
            select(MessageDelivery).where(
                MessageDelivery.target_root_id == root.id, MessageDelivery.status == "pending"
            )
        ):
            # 用户在等待审批时明确插入的新指令随原运行继续，不按异常恢复丢弃。
            delivery.target_generation = root.generation
        root.status, root.worker_id, root.stop_reason = "queued", None, None
        root.finished_at = None
        session.add(
            RunDispatch(run_id=root.id, generation=root.generation, next_attempt_at=jobs.now())
        )
        jobs._event(
            session, root, "approval_resumed", {"status": "queued", "generation": root.generation}
        )
        return jobs._view(root)


class ExecutionApprovals:
    def __init__(self, context):
        self.context = context

    def policy(self, call: dict):
        context = self.context
        root = context.jobs.get_run(context.root_id)
        workspace, cwd = workspace_paths(context)
        settings = ExecutionSettings(context.jobs.sessions)
        mode = (
            settings.get(root["conversation_id"])["permission_mode"]
            if root["conversation_id"]
            else "ask"
        )
        return evaluate_execution(
            "middleware:execute",
            mode=mode,
            command=call["args"].get("command", ""),
            cwd=cwd,
            rules=settings.rules(workspace, root["conversation_id"]),
        )

    def list(self) -> list[dict]:
        with self.context.jobs.sessions() as session:
            rows = session.scalars(
                select(ExecutionAction)
                .where(ExecutionAction.root_id == self.context.root_id)
                .order_by(
                    ExecutionAction.created_at, ExecutionAction.batch_key, ExecutionAction.position
                )
            )
            return [action_view(row) for row in rows]

    def _ensure(self, call: dict, state: dict, *, origin: str) -> dict:
        context = self.context
        workspace, cwd = workspace_paths(context)
        digest = execution_fingerprint(call, workspace=workspace, cwd=cwd)
        batch, ids = batch_identity(state)
        with context.jobs.sessions.begin() as session:
            root = context.jobs._locked(session, context.root_id)
            prior = session.scalar(
                select(ExecutionAction)
                .where(
                    ExecutionAction.run_id == context.run_id,
                    ExecutionAction.tool_call_id == call["id"],
                )
                .with_for_update()
            )
            if prior:
                if prior.fingerprint != digest:
                    raise PermissionError("待执行参数与原审批不一致，不能重用批准")
                return action_view(prior)
            defaults = (
                ExecutionSettings(context.jobs.sessions).get(root.conversation_id)
                if root.conversation_id
                else {"permission_mode": "ask", "revision": 0}
            )
            proposed = self.policy(call) if call["name"] == "execute" else None
            matched_rule = (
                session.get(ExecutionRule, proposed.rule_id)
                if proposed and proposed.rule_id
                else None
            )
            row = ExecutionAction(
                root_id=root.id,
                run_id=context.run_id,
                tool_call_id=call["id"],
                tool_name=call["name"],
                arguments=call["args"],
                fingerprint=digest,
                workspace=workspace,
                cwd=cwd,
                authority_epoch=root.authority_epoch,
                batch_key=batch,
                position=ids.index(call["id"]) if call["id"] in ids else 0,
                origin=origin,
                decision={
                    "proposed_under": {
                        "conversation_settings": defaults,
                        "rule_id": matched_rule.id if matched_rule else None,
                        "rule_revision": matched_rule.revision if matched_rule else None,
                    }
                },
            )
            session.add(row)
            session.flush()
            context.jobs._event(
                session,
                root,
                "approval_required",
                {"action_id": row.id, "run_id": context.run_id, "tool": row.tool_name},
            )
            return action_view(row)

    def should_interrupt(self, request) -> bool:
        """when 谓词重入时保持原批次；不能因批准新建了规则而跳过缓存中的 interrupt。"""
        call = request.tool_call
        with self.context.jobs.sessions() as session:
            prior = session.scalar(
                select(ExecutionAction).where(
                    ExecutionAction.run_id == self.context.run_id,
                    ExecutionAction.tool_call_id == call["id"],
                )
            )
            if prior is not None and prior.origin == "hitl":
                return True
        from knowledgeops_agent.harness.tools.access import check_tool_call

        try:
            check_tool_call(self.context, call["name"])
        except PermissionError:
            return False
        if self.policy(call).decision != "ask":
            return False
        self._ensure(call, request.state, origin="hitl")
        return True

    def before_tool(self, request) -> None:
        """最终执行关口，兼容直接停在旧工具节点或模型后才收窄的权限。"""
        call, context = request.tool_call, self.context
        if call["name"] != "execute":
            return
        current = self.policy(call)
        if current.decision == "deny":
            raise PermissionError(current.reason)
        with context.jobs.sessions() as session:
            root = session.get(Run, context.root_id)
            retry = session.scalar(
                select(CommandExecution).where(
                    CommandExecution.run_id == context.run_id,
                    CommandExecution.tool_call_id == call["id"],
                    CommandExecution.status == "retry_ready",
                )
            )
            workspace, cwd = workspace_paths(context)
            retry_granted = bool(
                retry
                and retry.authority_epoch == root.authority_epoch
                and retry.fingerprint == execution_fingerprint(call, workspace=workspace, cwd=cwd)
            )
            row = session.scalar(
                select(ExecutionAction).where(
                    ExecutionAction.run_id == context.run_id,
                    ExecutionAction.tool_call_id == call["id"],
                )
            )
            if row is not None:
                root = session.get(Run, context.root_id)
                workspace, cwd = workspace_paths(context)
                if (
                    row.fingerprint != execution_fingerprint(call, workspace=workspace, cwd=cwd)
                    or row.authority_epoch != root.authority_epoch
                ):
                    raise PermissionError("旧审批参数或用户指令已失效，未执行")
                if row.status in {"denied", "invalidated"}:
                    raise PermissionError("用户已拒绝或撤回此操作，请选择其他方案")
                if row.decision.get("rule_id"):
                    rule = session.get(ExecutionRule, row.decision["rule_id"])
                    if (
                        not rule
                        or not rule.enabled
                        or rule.deleted
                        or rule.revision != row.decision.get("rule_revision", 1)
                    ):
                        raise PermissionError("本次批准关联的命令规则已修改或撤回，未执行")
                origin = row.origin
            else:
                origin = None
        if (current.decision == "allow" or retry_granted) and origin is None:
            return
        if origin is None:
            self._ensure(call, request.state, origin="tool")
            origin = "tool"
        if origin == "tool":
            row = self._ensure(call, request.state, origin="tool")
            # 即使状态已批准也重复调用原位置，让 LangGraph 消费此位置的恢复值。
            interrupt({"execution_action_id": row["id"]})
        with context.jobs.sessions() as session:
            row = session.scalar(
                select(ExecutionAction).where(
                    ExecutionAction.run_id == context.run_id,
                    ExecutionAction.tool_call_id == call["id"],
                )
            )
            root = session.get(Run, context.root_id)
            if (
                row is None
                or row.status != "approved"
                or row.authority_epoch != root.authority_epoch
                or root.authority_epoch != root.applied_authority_epoch
            ):
                raise PermissionError("此动作没有有效的真人批准，未执行")
        if self.policy(call).decision == "deny":
            raise PermissionError("执行前规则已收窄，批准失效")

    def record_interrupts(self, snapshot) -> None:
        """把公开 StateSnapshot 的中断标识与原固定动作关联，前端不处理图协议。"""
        context = self.context
        batch, _ = batch_identity(snapshot.values)
        with context.jobs.sessions.begin() as session:
            context.jobs._locked(session, context.root_id)
            for item in snapshot.interrupts:
                value = item.value
                if not isinstance(value, dict) or not ({"execution_action_id", "action_requests"} & value.keys()):
                    continue
                if "execution_action_id" in value:
                    rows = [session.get(ExecutionAction, value["execution_action_id"])]
                else:
                    rows = list(
                        session.scalars(
                            select(ExecutionAction)
                            .where(
                                ExecutionAction.run_id == context.run_id,
                                ExecutionAction.batch_key == batch,
                                ExecutionAction.origin == "hitl",
                            )
                            .order_by(ExecutionAction.position)
                        )
                    )
                    if len(rows) != len(value.get("action_requests", [])):
                        raise ValueError("框架中断与固定审批批次不一致")
                for row in rows:
                    if row is None or row.run_id != context.run_id:
                        raise ValueError("审批中断不属于当前运行")
                    row.interrupt_id = item.id

    def resume_input(self, snapshot):
        if not snapshot.interrupts:
            return None
        self.record_interrupts(snapshot)
        answers = {}
        with self.context.jobs.sessions() as session:
            root = session.get(Run, self.context.root_id)
            for item in snapshot.interrupts:
                if not isinstance(item.value, dict) or not ({"execution_action_id", "action_requests"} & item.value.keys()):
                    continue
                rows = list(
                    session.scalars(
                        select(ExecutionAction)
                        .where(
                            ExecutionAction.run_id == self.context.run_id,
                            ExecutionAction.interrupt_id == item.id,
                        )
                        .order_by(ExecutionAction.position)
                    )
                )
                if not rows or any(row.status == "pending" for row in rows):
                    raise ApprovalWaiting("操作仍在等待批准，已保留原中断")
                decisions = [
                    {"type": "approve"}
                    if row.status == "approved" and row.authority_epoch == root.authority_epoch
                    else {
                        "type": "reject",
                        "message": "用户拒绝或原指令已变化，未执行；请结合最新要求继续。",
                    }
                    for row in rows
                ]
                answers[item.id] = (
                    {"decisions": decisions}
                    if "action_requests" in item.value
                    else {"decision": decisions[0]["type"]}
                )
        return Command(resume=answers)

    def decide(
        self,
        action_id: str,
        *,
        expected_fingerprint: str,
        approve: bool,
        scope: str = "once",
        match: str = "exact",
    ) -> dict:
        if scope not in {"once", "session", "persistent"} or (not approve and scope != "once"):
            raise ValueError("无效的审批作用域")
        context = self.context
        with context.jobs.sessions.begin() as session:
            root = context.jobs._locked(session, context.root_id)
            row = session.scalar(
                select(ExecutionAction)
                .where(ExecutionAction.id == action_id, ExecutionAction.root_id == root.id)
                .with_for_update()
            )
            if row is None:
                raise KeyError("approval action not found")
            if root.cancel_requested:
                raise PermissionError("运行已取消，旧审批不能恢复执行")
            if row.fingerprint != expected_fingerprint:
                raise ValueError("审批参数已变化，请刷新后核对")
            wanted = {"approve": approve, "scope": scope, "match": match}
            if row.status != "pending":
                if all(row.decision.get(key) == value for key, value in wanted.items()):
                    result = action_view(row)
                else:
                    raise ValueError("此审批已经处理，不能改写历史决定")
            else:
                owner = root if row.run_id == root.id else context.jobs._locked(session, row.run_id)
                root_open = root.status in {"interrupted", "paused"} or (
                    owner.id != root.id and root.status in {"running", "queued", "waiting_children", "waiting_input"}
                )
                if (not root_open or owner.status not in {"interrupted", "paused"}
                        or root.cancel_requested or owner.cancel_requested):
                    raise PermissionError("运行未处于待确认状态或已取消")
                if approve and (
                    root.authority_epoch != row.authority_epoch
                    or root.authority_epoch != root.applied_authority_epoch
                ):
                    raise PermissionError("用户指令已变化，原批准请求失效")
                if approve:
                    from knowledgeops_agent.harness.runtime.resources import (
                        ResourceService,
                        ResourceUnavailable,
                    )
                    from knowledgeops_agent.harness.tools.access import allowed_local_ids

                    resources = ResourceService(context.jobs.sessions)
                    snapshot = resources.read(root.id)
                    if not snapshot:
                        raise PermissionError("缺少原运行资源，不能批准")
                    bound = replace(context, run_id=row.run_id, resources=snapshot["content"])
                    try:
                        resources.validate(bound, bound.resources)
                    except ResourceUnavailable as error:
                        raise PermissionError(str(error)) from error
                    if "middleware:execute" not in allowed_local_ids(bound):
                        raise PermissionError("工具或角色许可已撤回，不能批准")
                    if (
                        ExecutionApprovals(bound)
                        .policy({"name": row.tool_name, "args": row.arguments})
                        .decision
                        == "deny"
                    ):
                        raise PermissionError("命令规则已明确拒绝，不能批准")
                rule_id = None
                if approve and scope != "once":
                    argv = reusable_command(row.arguments.get("command", ""))
                    if argv is None:
                        raise ValueError("此命令只能逐次批准")
                    validate_rule(
                        workspace=row.workspace, cwd=row.cwd, argv=argv, match=match, effect="allow"
                    )
                    rule = ExecutionRule(
                        workspace=row.workspace,
                        conversation_id=root.conversation_id if scope == "session" else None,
                        cwd=row.cwd,
                        argv=argv,
                        match=match,
                        effect="allow",
                        source_action_id=row.id,
                    )
                    session.add(rule)
                    session.flush()
                    rule_id = rule.id
                row.status = "approved" if approve else "denied"
                row.resolved_at = utcnow()
                row.decision = {
                    **row.decision,
                    **wanted,
                    "actor": "user",
                    "rule_id": rule_id,
                    "rule_revision": 1 if rule_id else None,
                    "at": row.resolved_at.isoformat(),
                }
                context.jobs._event(
                    session,
                    root,
                    "approval_resolved",
                    {"action_id": row.id, "approve": approve, "scope": scope},
                )
                result = action_view(row)
        queue_after_decisions(context.jobs, context.root_id)
        return result


def approval_middleware(context) -> HumanInTheLoopMiddleware:
    return HumanInTheLoopMiddleware(
        interrupt_on={
            "execute": {
                "allowed_decisions": ["approve", "reject"],
                "when": ExecutionApprovals(context).should_interrupt,
            }
        },
        description_prefix="本地命令执行前需要真人批准",
    )
