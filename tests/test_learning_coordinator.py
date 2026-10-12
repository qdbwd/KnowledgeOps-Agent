"""真实PG队列与回执合同；用小预算验证跨Run控制，不消耗供应商调用。"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_jobs import jobs as _jobs

from knowledgeops_agent.db import ChangeSet, Conversation, LearningAdvance, LearningUnit
from knowledgeops_agent.learning_budget import reserve_model
from knowledgeops_agent.learning_coordinator import advance
from knowledgeops_agent.learning_tasks import LearningTaskService
from knowledgeops_agent.model_gateway import ModelBudgetExceeded
from knowledgeops_agent.publication_intent import ensure_publication_intent

jobs = _jobs


def setup(jobs, units=None):
    _, _, service, sessions, _ = jobs
    conv = service.create_conversation("事务学习")
    tasks = LearningTaskService(sessions)
    task = tasks.create(
        conversation_id=conv["id"],
        request_key="task",
        title="事务",
        goal="理解事务",
        mode="delegated",
        units=units or [{"key": "one", "title": "原子性", "goal": "整体提交"}],
    )
    task = tasks.confirm(task["id"], expected_revision=1, auto_publish=True)
    run = service.list_runs()[0]
    return service, sessions, tasks, task, run


def test_automatic_run_has_no_user_identity_and_human_queue_takes_priority(jobs):
    service, sessions, _, task, automatic = setup(jobs)
    assert automatic["origin"] == "automatic" and automatic["conversation_sequence"] is None
    assert service.messages(task["conversation_id"]) == []
    human = service.enqueue(task["conversation_id"], "请先解释事务", request_key="human")
    assert service.claim("w", run_id=automatic["id"], generation=1) is None
    human = service.claim("w", run_id=human["id"], generation=1)
    assert human["execution_sequence"] == 1 and human["conversation_sequence"] == 1
    service.finish(human["id"], "w", outcome={"answer": "事务是一组操作"})
    claimed = service.claim("w", run_id=automatic["id"], generation=1)
    assert claimed["execution_sequence"] == 2 and claimed["continuation_snapshot_id"]
    # 自动推进不调用语义解释器，因而没有来源消息的提取或批准。
    context = SimpleNamespace(
        run_id=claimed["id"], root_id=claimed["id"], jobs=service, worker_id="w"
    )
    assert ensure_publication_intent(context)["origin"] == "automatic"
    service.finish(claimed["id"], "w", outcome={"answer": "当前单元说明"})
    with sessions() as session:
        conv = session.get(Conversation, task["conversation_id"])
        assert conv.user_sequence == 1 and conv.latest_execution_sequence == 2
        assert len(list(session.scalars(select(LearningAdvance)))) == 1


def test_atomic_finish_advances_independent_unit_and_never_replays(jobs):
    service, sessions, tasks, task, run = setup(
        jobs,
        [
            {"key": "one", "title": "一", "goal": "一"},
            {"key": "dependent", "title": "二", "goal": "二", "dependencies": ["one"]},
            {"key": "independent", "title": "三", "goal": "三"},
        ],
    )
    service.claim("w", run_id=run["id"], generation=1)
    # 无新草稿/报告的完成不能无限续跑：该单元受阻，依赖项不执行，独立项可推进。
    service.finish(run["id"], "w", outcome={"answer": "已思考"})
    service.finish(run["id"], "w", outcome={"answer": "重复通知"})
    with sessions.begin() as session:
        advance(session, task["conversation_id"])
        rows = list(session.scalars(select(LearningAdvance).order_by(LearningAdvance.sequence)))
        assert len(rows) == 2 and rows[1].unit_id == task["units"][2]["id"]
    units = tasks.get(task["id"])["units"]
    assert [u["status"] for u in units] == ["blocked", "dependency_blocked", "drafting"]


def test_pause_restart_and_resume_keep_budget_and_original_run(jobs):
    module, _, _, _, now = jobs
    service, sessions, tasks, task, run = setup(jobs)
    service.claim("w", run_id=run["id"], generation=1)
    service.reserve_tool(run["id"], "read_source")
    now[0] += timedelta(seconds=5)
    paused = tasks.control(task["id"], expected_revision=task["revision"], action="pause")
    with pytest.raises(module.RunStopped, match="暂停"):
        service.guard(run["id"])
    service.finish(run["id"], "w", status="interrupted", reason="用户暂停")
    resumed = tasks.control(task["id"], expected_revision=paused["revision"], action="resume")
    assert service.get_run(run["id"])["generation"] == 2
    service.claim("w", run_id=run["id"], generation=2)
    service.heartbeat(run["id"], "w")
    service.recover_on_startup()
    assert tasks.get(task["id"])["status"] == "waiting"
    with sessions.begin() as session:
        assert advance(session, task["conversation_id"]) is None
        unit = session.get(LearningUnit, run["learning_unit_id"])
        assert unit.tool_calls == 1 and unit.active_ms == 5000
    assert resumed["status"] == "active"


def test_unit_model_and_tool_budgets_survive_new_root_and_idempotent_reservation(jobs):
    module, _, _, _, _ = jobs
    service, sessions, _, task, run = setup(jobs)
    service.claim("w", run_id=run["id"], generation=1)
    with sessions.begin() as session:
        unit = session.get(LearningUnit, run["learning_unit_id"])
        unit.budgets = {**unit.budgets, "model_calls": 2, "tool_calls": 2, "searches": 1}
        draft = ChangeSet(
            root_id=run["id"], learning_unit_id=unit.id, operations=[], validated_evidence_ids=[]
        )
        session.add(draft)
        session.flush()
        unit.draft_id = draft.id
    reserve_model(service, "first", run["id"], run["id"])
    reserve_model(service, "first", run["id"], run["id"])
    service.reserve_tool(run["id"], "search", is_search=True)
    service.finish(run["id"], "w")
    later = next(r for r in service.list_runs() if r["status"] == "queued")
    service.claim("w", run_id=later["id"], generation=1)
    reserve_model(service, "second", later["id"], later["id"])
    with pytest.raises(ModelBudgetExceeded, match="unit"):
        reserve_model(service, "third", later["id"], later["id"])
    with pytest.raises(module.RunStopped, match="unit search"):
        service.reserve_tool(later["id"], "search", is_search=True)
    service.reserve_tool(later["id"], "read")
    with pytest.raises(module.RunStopped, match="unit tool"):
        service.reserve_tool(later["id"], "read")
    with sessions() as session:
        unit = session.get(LearningUnit, run["learning_unit_id"])
        assert (unit.model_calls, unit.tool_calls, unit.searches) == (2, 2, 1)


def test_pause_and_finish_use_one_control_lock_and_do_not_continue(jobs):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from knowledgeops_agent.learning_intent import BoundSessions

    service, sessions, tasks, task, run = setup(jobs)
    service.claim("w", run_id=run["id"], generation=1)
    entered = Event()

    def finish():
        entered.set()
        return service.finish(run["id"], "w", outcome={"answer": "保留当前进展"})

    with ThreadPoolExecutor(max_workers=1) as executor:
        with sessions.begin() as session:
            LearningTaskService._locked(session, task["id"])
            future = executor.submit(finish)
            assert entered.wait(3)
            LearningTaskService(BoundSessions(session)).control(
                task["id"], expected_revision=task["revision"], action="pause"
            )
        future.result(timeout=5)
    assert tasks.get(task["id"])["status"] == "paused"
    assert not any(r["status"] == "queued" for r in service.list_runs())


def test_waiting_unit_blocks_extra_tools_but_preserves_final_response(jobs):
    module, _, _, _, _ = jobs
    service, sessions, tasks, task, run = setup(jobs)
    service.claim("w", run_id=run["id"], generation=1)
    with sessions.begin() as session:
        unit = session.get(LearningUnit, run["learning_unit_id"])
        unit.status, unit.stop_reason = "waiting", "需要选择例子领域"
    with pytest.raises(module.RunStopped, match="等待真人"):
        service.reserve_tool(run["id"], "prepare_changes")
    service.guard(run["id"])
    service.finish(run["id"], "w", outcome={"answer": "请选择例子的领域"})
    assert tasks.get(task["id"])["status"] == "waiting"
    task = tasks.get(task["id"])
    tasks.control(task["id"], expected_revision=task["revision"], action="resume")
    assert len([r for r in service.list_runs() if r["status"] == "queued"]) == 1


def test_changed_requirements_resume_new_run_without_rewriting_old_snapshot(jobs):
    from knowledgeops_agent.db import RunResourceSnapshot

    service, sessions, tasks, task, run = setup(jobs)
    service.claim("w", run_id=run["id"], generation=1)
    service.reserve_tool(run["id"], "read_source")
    frozen = {"learning": {"task": {"requirements_revision": task["requirements_revision"]}}}
    with sessions.begin() as session:
        session.add(RunResourceSnapshot(root_id=run["id"], content=frozen, content_hash="frozen"))
    task = tasks.control(task["id"], expected_revision=task["revision"], action="pause")
    service.finish(run["id"], "w", status="interrupted", reason="暂停")
    task = tasks.configure(
        task["id"],
        expected_revision=task["revision"],
        changes={"examples": "新的SQL例子"},
        source={"kind": "manual"},
    )
    tasks.control(task["id"], expected_revision=task["revision"], action="resume")
    runs = service.list_runs()
    assert len(runs) == 2
    new = next(r for r in runs if r["id"] != run["id"])
    assert new["status"] == "queued" and new["learning_unit_id"] == run["learning_unit_id"]
    assert service.get_run(run["id"])["status"] == "interrupted"
    with sessions() as session:
        assert session.get(RunResourceSnapshot, run["id"]).content == frozen
        assert session.get(LearningUnit, run["learning_unit_id"]).tool_calls == 1
