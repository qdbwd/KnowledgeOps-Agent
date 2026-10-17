"""持久主子调度合同：真实隔离 PG，时间可控；不调用模型或依赖线程偶然排序。"""

from datetime import timedelta

import pytest
from sqlalchemy import select
from test_jobs import enqueue
from test_jobs import jobs as _jobs

from knowledgeops_agent.app.runs.children import ChildRuns
from knowledgeops_agent.persistence.models import ChildNotification, RunDispatch

jobs = _jobs


def family(service):
    root = enqueue(service)
    service.claim("main", run_id=root["id"], generation=1)
    first = service.start_child(root["id"], "独立核对来源", operation_key="child-first")
    second = service.start_child(root["id"], "独立检查边界", operation_key="child-second")
    return root, first, second


def test_child_creation_is_durable_queued_and_replay_does_not_dispatch_twice(jobs):
    _, _, service, sessions, _ = jobs
    root, first, second = family(service)
    assert first["status"] == second["status"] == "queued"
    assert first["id"] != second["id"]
    again = service.start_child(root["id"], first["task"], operation_key="child-first")
    assert again["id"] == first["id"]
    with sessions() as session:
        dispatches = list(session.scalars(select(RunDispatch).where(RunDispatch.run_id == first["id"])))
        assert len(dispatches) == 1 and dispatches[0].generation == 1
    with pytest.raises(ValueError, match="two"):
        service.start_child(root["id"], "第三个", operation_key="child-third")
    with pytest.raises(PermissionError, match="recursive"):
        service.start_child(first["id"], "不允许孙任务")


def test_root_and_two_children_claim_independently_and_wait_releases_root(jobs):
    _, _, service, _, _ = jobs
    root, first, second = family(service)
    assert service.claim("child-1", run_id=first["id"], generation=1)
    assert service.claim("child-2", run_id=second["id"], generation=1)
    assert service.claim("duplicate", run_id=first["id"], generation=1) is None
    other = enqueue(service, "other-conversation")
    assert service.claim("unrelated", run_id=other["id"], generation=1) is None
    waiting = ChildRuns(service).wait(root["id"], "main", [first["id"], second["id"]])
    assert waiting["status"] == "waiting_children"
    service.guard(first["id"], worker_id="child-1")
    service.guard(second["id"], worker_id="child-2")
    with pytest.raises(Exception, match="owner"):
        service.guard(root["id"], worker_id="main")
    assert service.claim("unrelated", run_id=other["id"], generation=1) is None


def test_completion_event_resumes_same_parent_once_after_all_children(jobs):
    _, _, service, sessions, _ = jobs
    root, first, second = family(service)
    service.claim("child-1", run_id=first["id"], generation=1)
    service.claim("child-2", run_id=second["id"], generation=1)
    ChildRuns(service).wait(root["id"], "main", [first["id"], second["id"]])
    service.finish(first["id"], "child-1", outcome={"answer": "第一份结果"})
    assert service.get_run(root["id"])["status"] == "waiting_children"
    service.finish(second["id"], "child-2", status="failed", reason="离线模拟失败")
    parent = service.get_run(root["id"])
    assert parent["status"] == "queued" and parent["generation"] == 2
    service.finish(second["id"], "child-2", status="failed", reason="重复完成投递")
    with sessions() as session:
        assert len(list(session.scalars(select(ChildNotification)))) == 2
        assert len(list(session.scalars(select(RunDispatch).where(
            RunDispatch.run_id == root["id"], RunDispatch.generation == 2,
        )))) == 1
    assert service.claim("main-next", run_id=root["id"], generation=2)


def test_group_clock_counts_overlap_once_and_stops_after_last_child(jobs):
    _, _, service, _, clock = jobs
    root = enqueue(service)
    service.claim("main", run_id=root["id"], generation=1)
    clock[0] += timedelta(seconds=5)
    child = service.start_child(root["id"], "核对", operation_key="timed-child")
    service.claim("child", run_id=child["id"], generation=1)
    clock[0] += timedelta(seconds=5)
    ChildRuns(service).wait(root["id"], "main", [child["id"]])
    clock[0] += timedelta(seconds=10)
    service.finish(child["id"], "child", outcome={"answer": "已核对"})
    assert service.get_run(root["id"])["active_ms"] == 20000
    clock[0] += timedelta(hours=1)
    assert service.get_run(root["id"])["active_ms"] == 20000
    service.claim("main-next", run_id=root["id"], generation=2)
    clock[0] += timedelta(seconds=2)
    assert service.finish(root["id"], "main-next")["active_ms"] == 22000


def test_root_cannot_finish_and_leave_running_children(jobs):
    _, _, service, _, _ = jobs
    root, _, _ = family(service)
    with pytest.raises(ValueError, match="子任务"):
        service.finish(root["id"], "main", outcome={"answer": "过早宣布完成"})
    assert service.get_run(root["id"])["status"] == "running"


def test_paused_parent_is_not_revived_by_child_result_and_resumes_children(jobs):
    module, _, service, _, _ = jobs
    root, first, second = family(service)
    service.claim("child-1", run_id=first["id"], generation=1)
    ChildRuns(service).wait(root["id"], "main", [first["id"], second["id"]])
    service.pause(root["id"])
    assert service.get_run(second["id"])["status"] == "paused"
    with pytest.raises(module.RunPaused):
        service.guard(first["id"], worker_id="child-1")
    service.settle_pause(first["id"], "child-1", outcome={})
    parent = service.resume(root["id"])
    assert parent["status"] == "queued" and parent["generation"] == 2
    assert service.get_run(first["id"])["status"] == "queued"
    assert service.get_run(second["id"])["status"] == "queued"
    assert service.claim("child-new", run_id=first["id"], generation=2)


def test_restart_interrupts_waiting_family_without_automatic_resume(jobs):
    _, _, service, _, _ = jobs
    root, first, second = family(service)
    service.claim("child-1", run_id=first["id"], generation=1)
    ChildRuns(service).wait(root["id"], "main", [first["id"], second["id"]])
    assert service.recover_on_startup() == 3
    assert all(service.get_run(row["id"])["status"] == "interrupted" for row in (root, first, second))
    assert service.claim("new-worker") is None


def test_failed_child_retries_only_when_original_call_reenters_after_human_resume(jobs):
    _, _, service, sessions, _ = jobs
    root, first, second = family(service)
    service.claim("child-1", run_id=first["id"], generation=1)
    service.claim("child-2", run_id=second["id"], generation=1)
    ChildRuns(service).wait(root["id"], "main", [first["id"], second["id"]])
    service.finish(first["id"], "child-1", status="failed", reason="确定失败")
    service.finish(second["id"], "child-2", outcome={"answer": "已有结果"})
    service.claim("event-resumed", run_id=root["id"], generation=2)
    failed = service.start_child(root["id"], first["task"], operation_key="child-first")
    assert failed["status"] == "failed" and failed["generation"] == 1
    service.finish(root["id"], "event-resumed", status="failed")
    resumed = service.resume(root["id"])
    service.claim("human-resumed", run_id=root["id"], generation=resumed["generation"])
    retried = service.start_child(root["id"], first["task"], operation_key="child-first")
    assert retried["id"] == first["id"] and retried["checkpoint_id"] == first["checkpoint_id"]
    assert retried["status"] == "queued" and retried["generation"] == resumed["generation"]
    assert service.get_run(second["id"])["status"] == "completed"
    service.start_child(root["id"], first["task"], operation_key="child-first")
    with sessions() as session:
        dispatches = list(session.scalars(select(RunDispatch).where(
            RunDispatch.run_id == first["id"], RunDispatch.generation == resumed["generation"],
        )))
        assert len(dispatches) == 1
