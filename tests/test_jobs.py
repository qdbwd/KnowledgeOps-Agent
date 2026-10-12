"""持久任务合同：不靠 Redis 或进程内对象获得恢复、权限和预算保证。"""

import importlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import select


@pytest.fixture
def jobs(business_sessions):
    module = importlib.import_module("knowledgeops_agent.jobs")
    db = importlib.import_module("knowledgeops_agent.db")
    sessions = business_sessions
    now = [datetime(2026, 10, 8, tzinfo=UTC)]
    service = module.RunService(sessions, now=lambda: now[0])
    yield module, db, service, sessions, now


def enqueue(service, key="request", **options):
    conversation = service.create_conversation("事务学习")
    return service.enqueue(conversation["id"], "读取资料并给出结论", request_key=key, **options)


def test_submission_retry_keeps_one_run_one_user_message_and_trusted_grant(jobs):
    _, db, service, sessions, _ = jobs
    conversation = service.create_conversation("写入")
    first = service.enqueue(
        conversation["id"], "整理", request_key="same", publication_mode="direct"
    )
    again = service.enqueue(
        conversation["id"], "整理", request_key="same", publication_mode="direct"
    )
    assert first["id"] == again["id"]
    assert first["checkpoint_id"] == first["id"] == first["root_id"]
    assert len(service.messages(conversation["id"])) == 1
    with sessions() as session:
        assert session.get(db.TaskGrant, first["root_id"]).mode == "direct"
    with pytest.raises(ValueError, match="idempotency"):
        service.enqueue(conversation["id"], "不同内容", request_key="same")


def test_atomic_claim_never_executes_same_root_twice(jobs):
    _, _, service, _, _ = jobs
    submitted = enqueue(service)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda i: service.claim(f"worker-{i}"), range(4)))
    assert [claim["id"] for claim in claims if claim] == [submitted["id"]]
    assert service.get_run(submitted["id"])["status"] == "running"


def test_restart_interrupts_running_and_queued_without_automatic_execution(jobs):
    _, _, service, _, now = jobs
    first = enqueue(service, "one")
    queued = enqueue(service, "two")
    service.claim("old-worker")
    now[0] += timedelta(seconds=5)
    # 恢复时只计算有心跳证据的活跃时间，随后一小时停机不会计入预算。
    running = next(run for run in service.list_runs() if run["status"] == "running")
    service.heartbeat(running["id"], "old-worker")
    now[0] += timedelta(hours=1)
    assert service.recover_on_startup() == 2
    assert service.claim("new-worker") is None
    assert service.get_run(queued["id"])["status"] == "interrupted"
    first = running
    assert service.get_run(first["id"])["active_ms"] == 5000
    # 用户等待一小时不占活跃执行预算；明确恢复仍沿用原 run/checkpoint。
    now[0] += timedelta(hours=1)
    resumed = service.resume(first["id"])
    assert resumed["id"] == first["id"] and resumed["active_ms"] == 5000
    assert service.claim("new-worker")["id"] == first["id"]


def test_cancel_blocks_subsequent_model_or_tool_admission(jobs):
    module, _, service, _, _ = jobs
    run = enqueue(service)
    service.claim("worker")
    service.cancel(run["id"])
    with pytest.raises(module.RunStopped, match="cancelled"):
        service.guard(run["id"])
    with pytest.raises(module.RunStopped):
        service.reserve_tool(run["id"], "read_source")
    service.finish(run["id"], "worker", status="cancelled", outcome={"kind": "partial"})
    assert service.get_run(run["id"])["status"] == "cancelled"


def test_child_limit_and_shared_tool_search_budget(jobs):
    module, _, service, _, _ = jobs
    run = enqueue(service, budgets={"tool_calls": 3, "searches": 1})
    service.claim("worker")
    one = service.start_child(run["id"], "查找机制")
    two = service.start_child(run["id"], "查找反例")
    with pytest.raises(ValueError, match="two"):
        service.start_child(run["id"], "第三个")
    with pytest.raises(PermissionError, match="recursive"):
        service.start_child(one["id"], "孙任务")
    service.reserve_tool(one["id"], "web_search", is_search=True)
    with pytest.raises(module.RunStopped, match="search"):
        service.reserve_tool(two["id"], "web_search", is_search=True)
    service.reserve_tool(two["id"], "read_source")
    service.reserve_tool(run["id"], "read_knowledge")
    with pytest.raises(module.RunStopped, match="tool"):
        service.reserve_tool(one["id"], "read_source")
    assert service.get_run(run["id"])["tool_calls"] == 3
    assert service.get_run(run["id"])["searches"] == 1


def test_parallel_tool_admission_refreshes_preloaded_run_under_lock(jobs, monkeypatch):
    """两个工具先读到同一旧值，再竞争行锁；只允许一个获得最后一次预算。"""
    module, _, service, _, _ = jobs
    run = enqueue(service, budgets={"tool_calls": 1})
    service.claim("worker")
    original = service._locked
    barrier = Barrier(2)

    def synchronized_lock(session, run_id):
        barrier.wait(timeout=5)
        return original(session, run_id)

    monkeypatch.setattr(service, "_locked", synchronized_lock)

    def reserve(_):
        try:
            service.reserve_tool(run["id"], "read_source")
        except module.RunStopped:
            return "exhausted"
        return "admitted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, range(2)))
    assert sorted(outcomes) == ["admitted", "exhausted"]
    assert service.get_run(run["id"])["tool_calls"] == 1
    events = service.events(run["id"])
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))


def test_time_budget_and_resume_do_not_reset_consumed_work(jobs):
    module, _, service, _, now = jobs
    run = enqueue(service, budgets={"active_seconds": 2, "tool_calls": 1})
    service.claim("worker")
    service.reserve_tool(run["id"], "read_source")
    now[0] += timedelta(seconds=3)
    with pytest.raises(module.RunStopped, match="time"):
        service.guard(run["id"])
    service.finish(run["id"], "worker", status="budget_exhausted", outcome={"kind": "partial"})
    with pytest.raises(ValueError, match="budget"):
        service.resume(run["id"])
    resumed = service.resume(run["id"], budgets={"active_seconds": 30, "tool_calls": 5})
    assert resumed["active_ms"] == 3000 and resumed["tool_calls"] == 1


def test_events_have_durable_cursor_and_finish_is_idempotent(jobs):
    _, _, service, _, _ = jobs
    run = enqueue(service)
    service.claim("worker")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: service.append_event(run["id"], "progress", {"step": i}), range(8)))
    events = service.events(run["id"])
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert service.events(run["id"], after=events[-1]["sequence"]) == []
    service.finish(run["id"], "worker", outcome={"kind": "answered", "answer": "结论"})
    service.finish(run["id"], "worker", outcome={"kind": "answered", "answer": "结论"})
    assert [m["role"] for m in service.messages(run["conversation_id"])] == ["user", "assistant"]
    with pytest.raises(PermissionError, match="owner"):
        service.heartbeat(run["id"], "other-worker")


def test_preview_and_child_runs_have_no_direct_publication_grant(jobs):
    _, db, service, sessions, _ = jobs
    run = enqueue(service, publication_mode="preview")
    service.claim("worker")
    child = service.start_child(run["id"], "研究")
    assert child["input"]["publication_mode"] == "preview"
    with sessions() as session:
        assert list(session.scalars(select(db.TaskGrant))) == []


def test_delegation_replay_preserves_child_identity_and_completed_result(jobs):
    _, _, service, _, _ = jobs
    root = enqueue(service)
    service.claim("worker")
    child = service.start_child(root["id"], "检查资料", operation_key="delegation:0")
    service.finish(child["id"], "worker", outcome={"answer": "已读证据"})
    again = service.start_child(root["id"], "检查资料", operation_key="delegation:0")
    assert again["id"] == child["id"] and again["status"] == "completed"
    assert again["outcome"]["answer"] == "已读证据"
    with pytest.raises(ValueError, match="different"):
        service.start_child(root["id"], "不同资料", operation_key="delegation:0")


def test_root_resume_reuses_interrupted_child_checkpoint_and_new_owner(jobs):
    _, _, service, _, _ = jobs
    root = enqueue(service)
    service.claim("old")
    child = service.start_child(root["id"], "检查资料", operation_key="delegation:1")
    service.recover_on_startup()
    resumed = service.resume(root["id"])
    service.claim("new", run_id=root["id"], generation=resumed["generation"])
    again = service.start_child(root["id"], "检查资料", operation_key="delegation:1")
    assert again["checkpoint_id"] == child["checkpoint_id"]
    assert again["generation"] == resumed["generation"]
    assert service.guard(again["id"], worker_id="new")["id"] == root["id"]


def test_cancel_during_last_model_call_cannot_finish_as_completed(jobs):
    _, _, service, _, _ = jobs
    root = enqueue(service)
    service.claim("worker")
    service.cancel(root["id"])
    result = service.finish(
        root["id"], "worker", outcome={"answer": "已经收到的文本", "published_ids": ["committed"]}
    )
    assert result["status"] == "cancelled"
    assert result["outcome"]["kind"] == "partial"
    assert result["outcome"]["published_ids"] == ["committed"]
