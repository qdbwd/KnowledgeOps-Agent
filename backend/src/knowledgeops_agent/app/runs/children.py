"""同一 worker 内的持久委派控制，不持有额外线程池或活的子图对象。

Run/dispatch 是执行事实，ChildNotification 是结果通知。等待与完成在同一
控制事务锁下交接，避免“子任务刚完成而主任务尚未落下等待”的丢失唤醒。
"""

from __future__ import annotations

import builtins

from sqlalchemy import and_, or_, select

from knowledgeops_agent.persistence.models import (
    ChildInstruction,
    ChildNotification,
    MessageDelivery,
    Run,
    RunControl,
    RunDispatch,
    RunEvent,
)

DONE = {"completed", "cancelled", "failed", "budget_exhausted", "interrupted"}
OPEN = {"queued", "running", "paused", "waiting_input", "waiting_children"}
HUMAN_WAIT = {"waiting_approval", "execution_unknown", "waiting_input"}


def is_unsettled(status: str, outcome_kind: str | None = None) -> bool:
    """兼容旧 interrupted 表达的人工等待；取消/失败后的旧 kind 不再代表待办。"""
    return status in OPEN or (status == "interrupted" and outcome_kind in HUMAN_WAIT)


def unsettled_children(session, root_id: str) -> builtins.list[Run]:
    """根事务锁下统一判断容量、取消传播与收束，避免各入口遗漏旧等待状态。"""
    return list(session.scalars(select(Run).where(
        Run.parent_id == root_id,
        or_(Run.status.in_(OPEN), and_(Run.status == "interrupted", Run.outcome["kind"].astext.in_(HUMAN_WAIT))),
    ).order_by(Run.id)))


class ChildrenWaiting(Exception):
    """图检查点已保存等待位置，worker 可以把槽位交还调度器。"""

    def __init__(self, ids, mode="all"):
        self.ids, self.mode = ids, mode
        super().__init__("等待子任务结果")


def child_view(jobs, row: Run) -> dict:
    return {"task_id": row.id, "run_id": row.id, "task": row.task,
            "agent_name": row.input_data.get("subagent", {}).get("name", "researcher"),
            "status": row.status, "answer": row.outcome.get("answer", ""),
            "error": row.stop_reason, "revision": row.authority_epoch,
            "last_event": row.event_sequence, "active_ms": jobs._active_ms(row),
            "cancel_requested": row.cancel_requested, "outcome_kind": row.outcome.get("kind"),
            "unsettled": is_unsettled(row.status, row.outcome.get("kind"))}


def control_for(session, run_id: str) -> RunControl:
    row = session.get(RunControl, run_id)
    if row is None:
        row = RunControl(run_id=run_id, pause_requested=False, revision=1, wait_spec={})
        session.add(row)
        session.flush()
    return row


def sync_group_clock(jobs, session, root: Run) -> None:
    """根时钟统计任一成员正在执行的时间并集，子时钟只供自身运行记录。"""
    session.flush()
    running = root.status == "running" or bool(session.scalar(select(Run.id).where(
        Run.parent_id == root.id, Run.status == "running",
    ).limit(1)))
    if running and root.active_since is None:
        root.active_since = jobs.now()
    elif not running and root.active_since is not None:
        jobs._pause_clock(root)
        from knowledgeops_agent.learning.budget import settle_time

        settle_time(session, root)


def _done(row: Run) -> bool:
    # 权限等待也曾使用 interrupted，不能把真人待处理事项伪装为子结果完成。
    return row.status in DONE and not is_unsettled(row.status, row.outcome.get("kind"))


def pending_guidance(session, root: Run) -> bool:
    """只有明确选择插入且尚未应用的真人消息才能提前唤醒，不读取普通排队。"""
    return bool(session.scalar(select(MessageDelivery.id).where(
        MessageDelivery.target_root_id == root.id,
        MessageDelivery.target_generation == root.generation,
        MessageDelivery.status == "pending",
    ).limit(1)))


def queue_if_ready(jobs, session, root: Run) -> bool:
    control = session.get(RunControl, root.id)
    if root.status != "waiting_children" or root.cancel_requested or (
        control is None or control.pause_requested
    ):
        return False
    spec = control.wait_spec.get("children")
    if not spec:
        return False
    session.flush()
    rows = list(session.scalars(select(Run).where(
        Run.parent_id == root.id, Run.id.in_(spec["ids"]),
    )))
    if len(rows) != len(spec["ids"]):
        raise ValueError("等待的子任务身份不完整")
    ready = any(map(_done, rows)) if spec["mode"] == "any" else all(map(_done, rows))
    guided = pending_guidance(session, root)
    if not ready and not guided:
        return False
    root.generation += 1
    root.status, root.worker_id, root.stop_reason = "queued", None, None
    for delivery in session.scalars(select(MessageDelivery).where(
        MessageDelivery.target_root_id == root.id, MessageDelivery.status == "pending",
    )):
        delivery.target_generation = root.generation
    session.add(RunDispatch(run_id=root.id, generation=root.generation, next_attempt_at=jobs.now()))
    jobs._event(session, root, "guidance_ready" if guided else "children_ready", {
        "status": "queued", "generation": root.generation,
    })
    return True


def child_finished(jobs, session, child: Run) -> None:
    """与子终态同事务保存通知；根正在运行时留待安全点消费，等待时仅投递一次。"""
    root = jobs._locked(session, child.root_id)
    prior = session.scalar(select(ChildNotification).where(
        ChildNotification.run_id == child.id, ChildNotification.generation == child.generation,
        ChildNotification.event_key == "finished",
    ))
    if prior is None:
        session.add(ChildNotification(
            root_id=root.id, run_id=child.id, generation=child.generation, event_key="finished",
            payload={"task_id": child.id, "status": child.status,
                     "summary": str(child.outcome.get("answer", ""))[:1200],
                     "result_ref": child.id, "error": child.stop_reason},
        ))
        jobs._event(session, root, "child_finished", {"child_id": child.id, "status": child.status})
    queue_if_ready(jobs, session, root)
    sync_group_clock(jobs, session, root)


def pause_queued_children(jobs, session, root: Run) -> None:
    for child in session.scalars(select(Run).where(Run.parent_id == root.id, Run.status.in_(OPEN))):
        if child.status != "running":
            control = control_for(session, child.id)
            control.wait_spec = {**control.wait_spec, "paused_from": child.status}
            child.status, child.worker_id = "paused", None
            child.stop_reason = "随根任务暂停，原子任务及检查点保留"
    sync_group_clock(jobs, session, root)


def resume_children(jobs, session, root: Run) -> None:
    for child in session.scalars(select(Run).where(
        Run.parent_id == root.id, Run.status.in_(["paused", "interrupted"]),
        Run.cancel_requested.is_(False),
    )):
        # 根任务的真人继续授权覆盖原组中因暂停/重启停止的子任务；不重试失败任务。
        child.generation = max(root.generation, child.generation + 1)
        child.status, child.worker_id, child.stop_reason = "queued", None, None
        child.budgets = root.budgets
        control = session.get(RunControl, child.id)
        if control:
            control.pause_requested = False
        session.add(RunDispatch(run_id=child.id, generation=child.generation, next_attempt_at=jobs.now()))
        jobs._event(session, child, "resumed", {"generation": child.generation, "parent_generation": root.generation})


def retry_failed_child(jobs, session, root: Run, child: Run) -> None:
    """旧同步调用重入可沿用真人恢复授权重试已结束失败；事件唤醒不获此授权。

    只在原工具再次请求同一子任务时继续，不重开其他历史失败子任务，也不自动
    重试 unknown。模型网关另行验证原请求是否已确定失败并保留原消费记录。
    """
    if child.status != "failed" or child.generation >= root.generation:
        return
    resumed = session.scalar(select(RunEvent).where(
        RunEvent.run_id == root.id, RunEvent.kind == "resumed",
    ).order_by(RunEvent.sequence.desc()).limit(1))
    if resumed is None or resumed.data.get("generation") != root.generation:
        return
    if len(unsettled_children(session, root.id)) >= 2:
        raise ValueError("at most two research children may run concurrently")
    child.generation = root.generation
    child.status, child.worker_id, child.stop_reason = "queued", None, None
    child.budgets = root.budgets
    session.add(RunDispatch(run_id=child.id, generation=child.generation, next_attempt_at=jobs.now()))
    jobs._event(session, child, "resumed", {
        "generation": child.generation, "parent_generation": root.generation,
        "reason": "原同步委派在真人恢复后重入，沿用原身份和预算",
    })


class ChildRuns:
    def __init__(self, jobs):
        self.jobs = jobs

    def list(self, root_id: str) -> builtins.list[dict]:
        if self.jobs.get_run(root_id)["parent_id"]:
            raise PermissionError("子任务不允许递归委派或管理子运行")
        with self.jobs.sessions() as session:
            return [child_view(self.jobs, row) for row in session.scalars(select(Run).where(
                Run.parent_id == root_id,
            ).order_by(Run.created_at, Run.id))]

    def get(self, root_id: str, child_id: str) -> dict:
        with self.jobs.sessions() as session:
            row = session.get(Run, child_id)
            if row is None or row.parent_id != root_id:
                raise KeyError("子任务不属于当前根运行")
            return child_view(self.jobs, row)

    def instructions(self, root_id: str, child_id: str) -> builtins.list[dict]:
        """供真人查看已发送要求；不自动把整段历史放入模型的任务状态返回值。"""
        self.get(root_id, child_id)
        with self.jobs.sessions() as session:
            return [{"id": row.id, "content": row.content, "origin": row.origin,
                     "status": row.status, "revision": row.authority_epoch}
                    for row in session.scalars(select(ChildInstruction).where(
                        ChildInstruction.run_id == child_id,
                    ).order_by(ChildInstruction.authority_epoch))]

    def update(self, root_id: str, child_id: str, message: str, *, request_key: str, origin: str) -> dict:
        if origin not in {"human", "agent"} or not request_key or not 1 <= len(message.strip()) <= 20000:
            raise ValueError("追加要求需要明确来源、请求身份及 1–20000 字符正文")
        with self.jobs.sessions.begin() as session:
            root = self.jobs._locked(session, root_id)
            child = self.jobs._locked(session, child_id)
            if root.parent_id or child.parent_id != root.id:
                raise PermissionError("不能操作其他根任务或递归委派")
            prior = session.scalar(select(ChildInstruction).where(
                ChildInstruction.run_id == child.id, ChildInstruction.request_key == request_key,
            ))
            if prior:
                if prior.content != message or prior.origin != origin:
                    raise ValueError("同一请求身份不能替换指令或来源")
                return child_view(self.jobs, child)
            if (root.cancel_requested or not is_unsettled(root.status, root.outcome.get("kind"))
                    or not is_unsettled(child.status, child.outcome.get("kind")) or child.cancel_requested):
                raise ValueError("原任务已结束，追加指令不能使其复活")
            child.authority_epoch += 1
            from knowledgeops_agent.app.runs.questions import invalidate_inputs

            invalidate_inputs(session, run_id=child.id)
            session.add(ChildInstruction(root_id=root.id, run_id=child.id, request_key=request_key,
                                         origin=origin, content=message, authority_epoch=child.authority_epoch))
            self.jobs._event(session, child, "child_instruction", {"origin": origin, "revision": child.authority_epoch})
            self.jobs._event(session, root, "child_updated", {"child_id": child.id, "origin": origin})
        from knowledgeops_agent.app.runs.questions import queue_after_answers

        queue_after_answers(self.jobs, root_id)
        return self.get(root_id, child_id)

    def cancel(self, root_id: str, child_id: str) -> dict:
        self.get(root_id, child_id)
        self.jobs.cancel(child_id)
        return self.get(root_id, child_id)

    def wake_for_guidance(self, root_id: str) -> None:
        with self.jobs.sessions.begin() as session:
            queue_if_ready(self.jobs, session, self.jobs._locked(session, root_id))

    def resume_answers(self, root_id: str, snapshot) -> dict:
        answers = {}
        with self.jobs.sessions() as session:
            for item in snapshot.interrupts:
                spec = item.value if isinstance(item.value, dict) else {}
                if "wait_child_ids" not in spec:
                    continue
                ids, mode = spec["wait_child_ids"], spec.get("mode", "all")
                rows = list(session.scalars(select(Run).where(Run.parent_id == root_id, Run.id.in_(ids))))
                if len(rows) != len(ids):
                    raise ValueError("等待检查点包含不属于当前任务的子运行")
                done = any(map(_done, rows)) if mode == "any" else all(map(_done, rows))
                root = session.get(Run, root_id)
                guided = root is not None and pending_guidance(session, root)
                if not done and not guided:
                    raise ChildrenWaiting(ids, mode)
                answers[item.id] = {"results": [child_view(self.jobs, row) for row in rows],
                                    "interrupted_by_guidance": guided}
        return answers

    def wait(self, root_id: str, worker_id: str, ids: builtins.list[str], *, mode: str = "all", outcome=None) -> dict:
        """图已保存等待位置后调用；这里只持久化等待和交接，不占住工作线程。"""
        if mode not in {"all", "any"} or not ids or len(set(ids)) != len(ids):
            raise ValueError("等待须指定不重复的子任务 ID，以及 all 或 any")
        with self.jobs.sessions.begin() as session:
            root = self.jobs._locked(session, root_id)
            if root.parent_id or root.worker_id != worker_id:
                raise PermissionError("等待必须由当前主运行的执行者提交")
            rows = list(session.scalars(select(Run).where(Run.parent_id == root.id, Run.id.in_(ids))))
            if len(rows) != len(ids):
                raise ValueError("不能等待其他根任务的子运行")
            control = control_for(session, root.id)
            control.wait_spec = {**control.wait_spec, "children": {"ids": ids, "mode": mode}}
            root.status = "cancelled" if root.cancel_requested else "paused" if control.pause_requested else "waiting_children"
            if root.status == "cancelled":
                root.finished_at = self.jobs.now()
            root.worker_id = None
            root.outcome = {**(outcome or {}), "kind": "waiting_children"}
            root.stop_reason = "等待子任务结果，主执行槽已释放" if root.status == "waiting_children" else "已按当前停止要求保留原等待位置"
            self.jobs._event(session, root, "waiting_children", {"status": root.status, "ids": ids, "mode": mode})
            queue_if_ready(self.jobs, session, root)
            sync_group_clock(self.jobs, session, root)
            return self.jobs._view(root)
