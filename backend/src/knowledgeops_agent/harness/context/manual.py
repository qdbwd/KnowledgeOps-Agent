"""真人主动压缩：复用运行队列、预算、摘要器与会话快照，不生成聊天任务或工具动作。"""

from langchain_core.messages import SystemMessage, messages_from_dict
from sqlalchemy import select

from knowledgeops_agent.app.lifecycle.access import require_conversation
from knowledgeops_agent.app.runs.control import lock_control
from knowledgeops_agent.app.runs.service import Budgets
from knowledgeops_agent.harness.context.continuation import snapshot_view
from knowledgeops_agent.harness.context.management import ContextMiddleware, working_messages
from knowledgeops_agent.persistence.models import ContinuationSnapshot, Run, RunDispatch, new_id


def _pending(session, conversation_id):
    return session.scalar(select(Run).where(
        Run.conversation_id == conversation_id, Run.parent_id.is_(None),
        (Run.status.in_(["queued", "running", "paused", "waiting_input", "waiting_children"]))
        | ((Run.status == "interrupted") & Run.outcome["kind"].astext.in_(["waiting_approval", "execution_unknown"])),
    ).order_by(Run.created_at, Run.id).limit(1))


def _history_count(snapshot):
    if snapshot is None:
        return 0
    return sum(not (item.get("data", {}).get("id") or "").startswith("summary-")
               for item in snapshot_view(snapshot)["content"].get("messages", []))


def status(jobs, gateway, conversation_id):
    with jobs.sessions() as session:
        conversation = require_conversation(session, conversation_id)
        pending = _pending(session, conversation_id)
        latest = session.scalar(select(Run).where(
            Run.conversation_id == conversation_id, Run.kind == "context_compaction",
        ).order_by(Run.created_at.desc(), Run.id.desc()).limit(1))
        snapshot = session.get(ContinuationSnapshot, conversation.latest_snapshot_id) if conversation.latest_snapshot_id else None
        count = _history_count(snapshot)
        reason = ("请先结束或取消当前任务，再压缩上下文。" if pending else
                  "模型调用已关闭，主动压缩需要摘要模型。" if not gateway.enabled else
                  "当前工作历史较短，暂时无需压缩。" if count <= 4 else "")
        return {"available": not bool(reason), "reason": reason,
                "run": jobs._view(latest) if latest else None}


def enqueue(jobs, gateway, conversation_id, request_key):
    if not 1 <= len(request_key) <= 200:
        raise ValueError("invalid request key")
    with jobs.sessions.begin() as session:
        lock_control(session)
        conversation = require_conversation(session, conversation_id)
        prior = session.scalar(select(Run).where(Run.request_key == request_key))
        if prior:
            if prior.kind != "context_compaction" or prior.conversation_id != conversation_id:
                raise ValueError("idempotency key belongs to a different request")
            return jobs._view(prior)
        if _pending(session, conversation_id):
            raise ValueError("请先结束或取消当前任务，再压缩上下文。")
        if not gateway.enabled:
            raise ValueError("模型调用已关闭，无法生成上下文摘要。")
        snapshot = session.get(ContinuationSnapshot, conversation.latest_snapshot_id) if conversation.latest_snapshot_id else None
        if _history_count(snapshot) <= 4:
            raise ValueError("当前工作历史较短，暂时无需压缩。")
        from knowledgeops_agent.harness.permissions.settings import runtime_inputs

        identity = new_id()
        run = Run(id=identity, root_id=identity, checkpoint_id=identity,
                  conversation_id=conversation_id, conversation_sequence=None,
                  request_key=request_key, kind="context_compaction", task="主动压缩会话上下文",
                  input_data={**runtime_inputs(session, conversation), "publication_mode": "preview",
                              "knowledge_only": False, "delegation": False, "auxiliary": False, "source_ids": []},
                  budgets=Budgets(model_requests=8, tool_calls=1, searches=0).model_dump(), created_at=jobs.now())
        session.add(run)
        session.flush()
        session.add(RunDispatch(run_id=run.id, generation=run.generation, next_attempt_at=jobs.now()))
        jobs._event(session, run, "queued", {"status": "queued", "operation": "context_compaction"})
        return jobs._view(run)


def execute(context):
    """只替换后续模型使用的工作视图；保存成功前不推进会话指针。

    最近完整模型请求用于容量估计；其详情已回收时不伪造完整请求百分比。
    原话、完整工具组、历史索引和引用由现有快照/生命周期服务继续管理。
    """
    snapshot = context.jobs.continuation(context.run_id)
    if not snapshot:
        raise ValueError("没有可压缩的会话上下文。")
    prior = snapshot["content"]
    state = {"messages": messages_from_dict(prior.get("messages", [])),
             "working_summary": prior.get("summary", ""),
             "task_notes": prior.get("task_notes", {}), "task_history": prior.get("task_history", {})}
    record = context.gateway.ledger.latest_agent_request(snapshot["run_id"])
    ancestor = snapshot.get("parent_snapshot_id")
    with context.jobs.sessions() as session:
        for _ in range(30):
            if record or not ancestor:
                break
            older = session.get(ContinuationSnapshot, ancestor)
            if older is None:
                break
            record = context.gateway.ledger.latest_agent_request(older.run_id)
            ancestor = older.parent_snapshot_id
    packet = record["request"] if record else {}
    previous = messages_from_dict([{"type": item["type"], "data": item} for item in packet.get("messages", [])])
    fixed = [item for item in previous if isinstance(item, SystemMessage)
             or item.additional_kwargs.get("knowledgeops_context_data")]

    def measure(messages):
        return context.gateway.measure([*fixed, *messages], tools=packet.get("tools", []), **packet.get("parameters", {}))

    update, event = ContextMiddleware()._compact(context, state, state["messages"], measure, strict=False, force=True)
    if update is None:
        failures = [item for item in context.jobs.events(context.run_id) if item["kind"] == "context_summary_failed"]
        if failures:
            raise ValueError("摘要未成功完成，原上下文保持不变。")
        return {"kind": "context_compaction_skipped", "message": event.get("skipped", "没有可压缩的早期完整消息。")}, None
    context.jobs.guard(context.run_id, worker_id=context.worker_id)
    if record and context.gateway.capacity.input_budget_version:
        event["after"]["components"] = context.gateway.measure_components(
            [*fixed, *working_messages(state["messages"], update["context_compaction"])],
            tools=packet.get("tools", []), **packet.get("parameters", {}),
        )
    context.jobs.append_event(context.run_id, "context_compacted", {
        **event, "trigger": "manual", "full_input_estimate": bool(record),
    })
    return {"kind": "context_compacted", "message": "上下文已压缩，原聊天记录保留。"}, {**state, **update}
