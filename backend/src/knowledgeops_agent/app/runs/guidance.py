"""显式引导的持久投递与安全点协议；普通排队消息不会进入当前任务。

请求引导先锁当前root并撤下写入许可。模型调用不能持有事务锁，解释完成后
再校验代次/授权epoch。新消息与发布争锁：先提交的事务决定是否已发生写入。
"""

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from knowledgeops_agent.harness.sandbox.receipts import invalidate_prepared_files
from knowledgeops_agent.persistence.models import ChatMessage, MessageDelivery, RunEvent, TaskGrant


def view(delivery: MessageDelivery) -> dict:
    return {
        "id": delivery.id,
        "message_id": delivery.message_id,
        "source_run_id": delivery.source_run_id,
        "target_root_id": delivery.target_root_id,
        "target_generation": delivery.target_generation,
        "authority_epoch": delivery.authority_epoch,
        "user_sequence": delivery.user_sequence,
        "status": delivery.status,
        "result": delivery.result,
    }


def request_guidance(jobs, queued_id: str, *, target_id: str, target_generation: int) -> dict:
    """用户入口的转换是原子的；错误目标保持原消息排队，重复转换返回同一回执。"""
    if queued_id == target_id:
        raise ValueError("引导消息必须来自独立的排队消息")
    with jobs.sessions.begin() as session:
        target = jobs._locked(session, target_id)
        queued = jobs._locked(session, queued_id)
        prior = session.scalar(
            select(MessageDelivery).where(MessageDelivery.source_run_id == queued_id)
        )
        if prior:
            if prior.target_root_id != target_id or target_generation not in {
                prior.target_generation,
                queued.outcome.get("target_generation"),
            }:
                raise ValueError("该消息已投递给另一个运行代次")
            return view(prior)
        waiting = (
            target.status == "interrupted" and target.outcome.get("kind") == "waiting_approval"
        )
        if (
            target.parent_id
            or (target.status not in {"running", "paused", "waiting_input", "waiting_children"} and not waiting)
            or target.cancel_requested
            or target.generation != target_generation
        ):
            raise ValueError("目标运行或代次已变化，原消息仍保持排队")
        if (
            queued.parent_id
            or queued.kind != "agent"
            or queued.status != "queued"
            or queued.origin != "human"
        ):
            raise ValueError("只有未执行的排队真人消息可以转为引导")
        if not queued.conversation_id or queued.conversation_id != target.conversation_id:
            raise ValueError("只能引导同一会话的当前运行")
        message = session.scalar(
            select(ChatMessage).where(ChatMessage.run_id == queued_id, ChatMessage.role == "user")
        )
        if message is None:
            raise ValueError("原始真人消息缺失")
        target.authority_epoch += 1
        from knowledgeops_agent.app.runs.questions import invalidate_inputs
        from knowledgeops_agent.persistence.models import ExecutionAction

        was_waiting_input = target.status == "waiting_input"
        was_waiting_children = target.status == "waiting_children"
        invalidate_inputs(session, root_id=target.id)

        for action in session.scalars(
            select(ExecutionAction).where(
                ExecutionAction.root_id == target.id,
                ExecutionAction.status.in_(["pending", "reviewing", "approved"]),
            )
        ):
            action.status = "invalidated"
        invalidate_prepared_files(session, root_id=target.id, reason="新的用户引导撤回原文件操作")
        delivery = MessageDelivery(
            message_id=message.id,
            source_run_id=queued_id,
            target_root_id=target.id,
            target_generation=target.generation,
            authority_epoch=target.authority_epoch,
            user_sequence=message.user_sequence,
        )
        session.add(delivery)
        session.flush()
        # 原任务不再执行，不能保留它自己的direct。目标root的许可先挂起，等待新指令解释。
        session.execute(delete(TaskGrant).where(TaskGrant.root_id.in_([target.id, queued.id])))
        queued.status, queued.cancel_requested = "cancelled", True
        queued.stop_reason = "消息已转为当前运行的引导，不再作为独立任务执行"
        queued.finished_at = jobs.now()
        queued.outcome = {
            "kind": "guidance",
            "delivery_id": delivery.id,
            "target_root_id": target.id,
            "target_generation": target_generation,
        }
        jobs._event(session, queued, "guidance_routed", view(delivery))
        jobs._event(session, target, "guidance_pending", view(delivery))
        result = view(delivery)
    if waiting:
        from knowledgeops_agent.harness.permissions.approvals import queue_after_decisions

        queue_after_decisions(jobs, target_id)
    elif was_waiting_input:
        from knowledgeops_agent.app.runs.questions import queue_after_answers

        queue_after_answers(jobs, target_id)
    elif was_waiting_children:
        from knowledgeops_agent.app.runs.children import ChildRuns

        ChildRuns(jobs).wake_for_guidance(target_id)
    return result


def deliveries(jobs, root_id: str) -> list[dict]:
    with jobs.sessions() as session:
        return [
            view(row)
            for row in session.scalars(
                select(MessageDelivery)
                .where(MessageDelivery.target_root_id == root_id)
                .order_by(MessageDelivery.authority_epoch)
            )
        ]


def invalidate_on_resume(jobs, session, run) -> None:
    """旧代次未应用的投递不会暗中进入新代次；界面保留失效原话，用户可重新选择。"""
    stale = list(
        session.scalars(
            select(MessageDelivery).where(
                MessageDelivery.target_root_id == run.id, MessageDelivery.status == "pending"
            )
        )
    )
    for delivery in stale:
        delivery.status = "stale"
        delivery.result = {"reason": "目标已恢复到新代次，此条未应用；如仍需引导请重新发送并选择。"}
        jobs._event(session, run, "guidance_stale", view(delivery))
    if stale:
        session.execute(delete(TaskGrant).where(TaskGrant.root_id == run.id))
        run.applied_authority_epoch = run.authority_epoch
        jobs._event(
            session,
            run,
            "publication_intent",
            {
                "mode": "ask",
                "published_ids": [],
                "continue_task": True,
                "authority_epoch": run.authority_epoch,
                "authorization_error": "旧代次引导未应用，写入许可已撤下。",
            },
        )


def pending_payload(jobs, delivery: dict) -> dict:
    """每条真实原话独立解释；默认模式来自本root已应用决定，不从模型历史恢复。"""
    with jobs.sessions() as session:
        message = session.get(ChatMessage, delivery["message_id"])
        queued = session.scalar(
            select(RunEvent).where(
                RunEvent.run_id == delivery["source_run_id"], RunEvent.kind == "queued"
            )
        )
        frozen = queued.data.get("publication_context", {}) if queued else {}
        events = list(
            session.scalars(
                select(RunEvent)
                .where(
                    RunEvent.run_id == delivery["target_root_id"],
                    RunEvent.kind == "publication_intent",
                )
                .order_by(RunEvent.sequence.desc())
            )
        )
        prior: dict = next(
            (
                event.data
                for event in events
                if event.data.get("authority_epoch", 0) < delivery["authority_epoch"]
            ),
            {},
        )
        root = jobs.get_run(delivery["target_root_id"])
        return {
            "message": {"id": message.id, "content": message.content},
            "root_id": delivery["target_root_id"],
            "ui_mode": prior.get("mode", root["input"]["publication_mode"]),
            "candidates": frozen.get("candidates", []),
            "candidates_truncated": frozen.get("candidates_truncated", False),
            "known_preferences": frozen.get("known_preferences", []),
            **({"memory_categories": frozen["memory_categories"]} if "memory_categories" in frozen else {}),
            "learning_context": frozen.get("learning_context", {}),
        }


def apply_decision(context, delivery: dict, result: dict) -> dict:
    """解释结果仅在相同运行代次和顺序内生效；比它新的消息仍保持写入屏障。"""
    jobs = context.jobs
    with jobs.sessions.begin() as session:
        run = jobs._active_root_guard(session, context.root_id, context.worker_id)
        row = session.get(MessageDelivery, delivery["id"])
        if row is None or row.target_root_id != run.id:
            raise ValueError("引导记录不存在")
        if row.status == "applied":
            return row.result
        if row.status != "pending" or row.target_generation != run.generation:
            raise ValueError("引导的目标代次已失效")
        earlier = session.scalar(
            select(MessageDelivery.id)
            .where(
                MessageDelivery.target_root_id == run.id,
                MessageDelivery.status == "pending",
                MessageDelivery.authority_epoch < row.authority_epoch,
            )
            .limit(1)
        )
        if earlier:
            raise ValueError("必须先处理较早的引导")
        result = {**result, "authority_epoch": row.authority_epoch}
        row.status, row.result = "applied", result
        run.applied_authority_epoch = row.authority_epoch
        session.execute(delete(TaskGrant).where(TaskGrant.root_id == run.id))
        if result["mode"] == "direct" and run.authority_epoch == row.authority_epoch:
            session.execute(insert(TaskGrant).values(root_id=run.id, mode="direct", actor="user"))
        jobs._event(session, run, "publication_intent", result)
        jobs._event(session, run, "guidance_applied", view(row))
    from knowledgeops_agent.app.runs.questions import queue_after_answers

    queue_after_answers(jobs, context.root_id)
    return result


def applied_messages(jobs, root_id: str) -> list[dict]:
    """原始真人消息与应用回执配对。撤回/失败的投递不伪装为已看到的指令。"""
    with jobs.sessions() as session:
        rows = session.execute(
            select(MessageDelivery, ChatMessage)
            .join(ChatMessage, ChatMessage.id == MessageDelivery.message_id)
            .where(
                MessageDelivery.target_root_id == root_id,
                MessageDelivery.status == "applied",
            )
            .order_by(MessageDelivery.authority_epoch)
        )
        return [
            {
                "id": message.id,
                "content": message.content,
                "sequence": delivery.user_sequence,
                "authority_epoch": delivery.authority_epoch,
            }
            for delivery, message in rows
        ]


class GuidanceMiddleware(AgentMiddleware):
    """复用LangChain模型前后钩子，事务回执与确定消息ID弥合DB/checkpoint提交间隙。

    新指令在before_model处理；若在模型请求中到达，after_model取消尚未执行的
    工具提案，补齐明确未执行的ToolMessage，再回到安全点。不会伪称撤销已执行动作。
    """

    def before_model(self, state, runtime):
        from knowledgeops_agent.knowledge.intent import (
            interpret_user_intent,
            publish_approved_intent,
        )

        context = runtime.context
        if context.run_id != context.root_id:
            return None
        root = context.jobs.guard(context.run_id, worker_id=context.worker_id)
        for delivery in deliveries(context.jobs, context.root_id):
            if (
                delivery["status"] == "pending"
                and delivery["target_generation"] == root["generation"]
            ):
                result = interpret_user_intent(context, pending_payload(context.jobs, delivery))
                apply_decision(context, delivery, result)
        result = context.jobs.publication_intent(context.root_id)
        if result:
            publish_approved_intent(context, result)
        # 即使DB已应用、图节点尚未checkpoint就崩溃，也从真实回执重新构造同ID消息。
        known = {message.id for message in state["messages"]}
        applied = applied_messages(context.jobs, context.root_id)
        messages = [
            HumanMessage(row["content"], id="guidance-" + row["id"])
            for row in applied
            if "guidance-" + row["id"] not in known
        ]
        latest = context.jobs.guard(context.run_id, worker_id=context.worker_id)
        update = {"decision_authority_epoch": latest["applied_authority_epoch"]}
        if messages:
            update.update(
                {
                    "messages": messages,
                    "guidance_seen_sequence": max(row["sequence"] for row in applied),
                    "final_response_feedback": [],
                    "answer_repair_count": 0,
                }
            )
        return update

    def _pending(self, context) -> bool:
        if context.run_id != context.root_id:
            return False
        root = context.jobs.guard(context.run_id, worker_id=context.worker_id)
        return root["authority_epoch"] != root["applied_authority_epoch"]

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime):
        if not self._pending(runtime.context):
            return None
        last = state["messages"][-1]
        calls = last.tool_calls if isinstance(last, AIMessage) else []
        if any(not call.get("id") for call in calls):
            raise ValueError("模型工具提议缺少调用ID，不能可靠构造引导后的协议记录")
        return {
            "messages": [
                ToolMessage(
                    "未执行：收到新的用户引导，先重新判断下一步。",
                    tool_call_id=call["id"],
                    name=call["name"],
                    status="error",
                    id=f"guidance-skip-{call['id']}",
                )
                for call in calls
            ],
            "jump_to": "model",
            "final_response_feedback": [],
        }

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state, runtime):
        # 最后一次模型钩子之后、终态之前到达的消息仍有一次安全点机会。
        return {"jump_to": "model"} if self._pending(runtime.context) else None
