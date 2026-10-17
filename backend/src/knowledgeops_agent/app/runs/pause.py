"""可接续暂停的持久控制：请求只设标志，执行者在图安全边界确认停下。"""

from knowledgeops_agent.app.runs.children import pause_queued_children, sync_group_clock
from knowledgeops_agent.persistence.models import Run, RunControl


def request_pause(jobs, run_id: str) -> dict:
    with jobs.sessions.begin() as session:
        run = jobs._locked(session, run_id)
        if run.parent_id or run.kind != "agent":
            raise ValueError("暂停入口用于根 Agent 任务；子任务随根任务暂停")
        if run.status not in {"running", "queued", "paused", "waiting_input", "waiting_children", "interrupted"}:
            raise ValueError("当前运行已结束，不能请求暂停")
        control = session.get(RunControl, run.id)
        if control is None:
            control = RunControl(run_id=run.id, pause_requested=False, revision=0, wait_spec={})
            session.add(control)
        if control.pause_requested:
            return jobs._view(run)
        control.pause_requested = True
        control.revision += 1
        control.updated_at = jobs.now()
        if run.status != "running":
            control.wait_spec = {**control.wait_spec, "paused_from": run.status}
            run.status, run.stop_reason = "paused", "用户已暂停；原检查点和待处理事项保留"
            run.worker_id = None
        pause_queued_children(jobs, session, run)
        session.flush()
        jobs._event(session, run, "pause_requested", {"status": run.status, "pause_requested": True})
        return jobs._view(run)


def settle_pause(jobs, run_id: str, worker_id: str, *, outcome: dict) -> dict:
    """只在图已保存节点状态后调用，不把暂停当完成，也不写入一条虚假最终答复。"""
    cancelled = False
    with jobs.sessions.begin() as session:
        run = jobs._locked(session, run_id)
        if run.worker_id != worker_id:
            raise PermissionError("worker owner does not match")
        if run.cancel_requested:
            cancelled = True
        else:
            if run.parent_id:
                jobs._pause_clock(run)
            from knowledgeops_agent.learning.budget import settle_time

            settle_time(session, run)
            run.status, run.stop_reason = "paused", "已在安全边界暂停，可从原位置继续"
            run.outcome, run.worker_id = {**outcome, "kind": "paused"}, None
            root = session.get(Run, run.root_id) if run.parent_id else run
            sync_group_clock(jobs, session, root)
            jobs._event(session, run, "paused", {"status": "paused"})
            result = jobs._view(run)
    if cancelled:
        return jobs.finish(run_id, worker_id, status="cancelled", outcome=outcome)
    return result
