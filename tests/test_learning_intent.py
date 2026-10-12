"""学习聊天桥使用真实事务、文件和运行；语义候选是固定响应，不调用供应商。"""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from knowledgeops_agent.db import LearningTask, OperationReceipt, Run
from knowledgeops_agent.file_store import FileStore
from knowledgeops_agent.jobs import RunService
from knowledgeops_agent.learning_intent import apply_candidates, current_learning
from knowledgeops_agent.learning_profiles import LearningProfileService
from knowledgeops_agent.learning_tasks import LearningTaskService


@pytest.fixture
def learning_context(business_sessions, tmp_path):
    business_sessions.configure(info={"file_store": FileStore(tmp_path)})
    jobs = RunService(business_sessions)
    tasks = LearningTaskService(business_sessions)
    conversation = jobs.create_conversation("学习")
    task = tasks.create(
        conversation_id=conversation["id"],
        request_key="task",
        title="数据库",
        goal="理解数据库事务",
    )
    profiles = LearningProfileService(business_sessions)
    profile = profiles.get(task_id=task["id"])
    profiles.update(profile["id"], expected_revision=1, content="保留这段人工说明。\n先讲概念。")
    run = jobs.enqueue(
        conversation["id"], "本任务多用SQL例子，把先讲概念改为先举例。", request_key="message"
    )
    jobs.claim("test", run_id=run["id"], generation=1)
    context = SimpleNamespace(
        jobs=jobs, root_id=run["id"], run_id=run["id"], worker_id="test", resources={}
    )
    return context, jobs.publication_context(run["id"]), tasks, profiles


def candidate(payload, action, **extra):
    return {"action": action, "clear": True, "quote": payload["message"]["content"], **extra}


def test_task_requirements_and_manual_patch_are_atomic_idempotent_and_visible(learning_context):
    context, payload, tasks, profiles = learning_context
    task_id = payload["learning_context"]["current_task_id"]
    items = [
        candidate(
            payload,
            "configure",
            target_id="current",
            expected_revision=1,
            fields={"examples": "多用SQL例子"},
        ),
        candidate(
            payload,
            "profile_edit",
            target_id="task_profile",
            expected_revision=2,
            old_text="先讲概念。",
            new_text="先举例。",
        ),
    ]
    result = apply_candidates(context, payload, items)
    assert result["errors"] == []
    assert apply_candidates(context, payload, items) == result
    assert len(tasks.history(task_id)) == 2
    assert profiles.get(task_id=task_id)["content"] == "保留这段人工说明。\n先举例。"
    overlay = current_learning(context)
    assert (
        overlay["task"]["configuration"]["examples"]["source"]["message_id"]
        == payload["message"]["id"]
    )
    assert overlay["profiles"]["task_configuration"]["examples"]["value"] == "多用SQL例子"
    with context.jobs.sessions() as session:
        assert len(list(session.scalars(select(OperationReceipt)))) == 2


def test_stale_profile_does_not_overwrite_ui_edit_or_rollback_good_task_choice(learning_context):
    context, payload, tasks, profiles = learning_context
    task_id = payload["learning_context"]["current_task_id"]
    context.resources["learning"] = {
        "task": tasks.get(task_id), "profiles": profiles.freeze(task_id=task_id, query="学习")
    }
    profile = profiles.get(task_id=task_id)
    profiles.update(profile["id"], expected_revision=2, content="用户刚在页面编辑的正文。")
    result = apply_candidates(
        context,
        payload,
        [
            candidate(
                payload,
                "profile_edit",
                target_id="task_profile",
                expected_revision=2,
                new_text="追加",
            ),
            candidate(
                payload,
                "configure",
                target_id="current",
                expected_revision=1,
                fields={"examples": "SQL"},
            ),
        ],
    )
    assert len(result["errors"]) == 1 and len(result["applied"]) == 1
    assert profiles.get(task_id=task_id)["content"] == "用户刚在页面编辑的正文。"
    assert tasks.get(task_id)["configuration"]["examples"]["value"] == "SQL"
    assert next(p for p in current_learning(context)["profiles"]["profiles"] if p["task_id"] == task_id)["content"] == "保留这段人工说明。\n先讲概念。"


def test_automatic_source_and_unknown_target_cannot_change_learning(learning_context):
    context, payload, tasks, _ = learning_context
    wrong = candidate(
        payload, "configure", target_id="another-task", expected_revision=1, fields={"focus": "x"}
    )
    assert apply_candidates(context, payload, [wrong])["errors"]
    with context.jobs.sessions.begin() as session:
        session.get(Run, context.root_id).origin = "learning_auto"
    valid = candidate(
        payload, "configure", target_id="current", expected_revision=1, fields={"focus": "x"}
    )
    assert apply_candidates(context, payload, [valid])["errors"]
    assert tasks.get(payload["learning_context"]["current_task_id"])["configuration"] == {}


def test_current_message_can_create_configure_and_confirm_bounded_task(learning_context):
    context, payload, tasks, _ = learning_context
    items = [
        candidate(
            payload,
            "create",
            fields={
                "title": "SQL例子",
                "goal": "通过SQL理解事务",
                "mode": "delegated",
                "units": [{"key": "u1", "title": "隔离", "goal": "解释隔离现象"}],
            },
        ),
        candidate(payload, "configure", target_id="created", fields={"examples": "SQL"}),
        candidate(payload, "confirm", target_id="created", auto_publish=True),
    ]
    result = apply_candidates(context, payload, items)
    assert result["errors"] == []
    assert apply_candidates(context, payload, items) == result
    created = result["applied"][-1]["task"]
    assert created["status"] == "active" and created["confirmed_requirements_revision"] == 2
    assert len(context.jobs.messages(created["conversation_id"])) == 1
    assert context.jobs.get_run(context.root_id)["learning_task_id"] == created["id"]
    with context.jobs.sessions() as session:
        assert len(list(session.scalars(select(LearningTask)))) == 2


def test_profile_patch_failure_rolls_back_without_receipt(learning_context):
    context, payload, _, profiles = learning_context
    task_id = payload["learning_context"]["current_task_id"]
    result = apply_candidates(
        context,
        payload,
        [
            candidate(
                payload,
                "profile_edit",
                target_id="task_profile",
                expected_revision=2,
                old_text="不存在的旧段落",
                new_text="不能整篇替换",
            )
        ],
    )
    assert result["errors"] and profiles.get(task_id=task_id)["revision"] == 2
    with context.jobs.sessions() as session:
        assert list(session.scalars(select(OperationReceipt))) == []


def test_task_configuration_refines_without_new_scope_authorization(learning_context):
    _, payload, tasks, profiles = learning_context
    task_id = payload["learning_context"]["current_task_id"]
    confirmed = tasks.confirm(task_id, expected_revision=1, auto_publish=True)
    configured = tasks.configure(
        task_id,
        expected_revision=confirmed["revision"],
        changes={"detail": "解释因果关系"},
        source={"kind": "manual"},
    )
    assert configured["auto_publish"]
    assert configured["confirmed_requirements_revision"] == configured["requirements_revision"]
    with pytest.raises(ValueError):
        tasks.configure(
            task_id,
            expected_revision=configured["revision"],
            changes={"budget": 999},
            source={"kind": "manual"},
        )
    exported = profiles.export_effective(task_id=task_id)
    assert "解释因果关系" in exported and "保留这段人工说明" in exported
