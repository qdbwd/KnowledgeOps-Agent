"""两层配置使用真实PG和临时文件；人工正文、来源和恢复不依赖模型。"""

import importlib

import pytest

from knowledgeops_agent.file_store import FileStore
from knowledgeops_agent.jobs import RunService
from knowledgeops_agent.preferences import MemoryService


@pytest.fixture
def learning(business_sessions, tmp_path):
    business_sessions.configure(info={"file_store": FileStore(tmp_path)})
    tasks = importlib.import_module("knowledgeops_agent.learning_tasks")
    profiles = importlib.import_module("knowledgeops_agent.learning_profiles")
    return (
        tasks.LearningTaskService(business_sessions),
        profiles.LearningProfileService(business_sessions),
        RunService(business_sessions),
        MemoryService(business_sessions),
    )


def task_in(learning, key="one", conversation_id=None):
    tasks, _, jobs, _ = learning
    conversation_id = conversation_id or jobs.create_conversation("学习")["id"]
    return tasks.create(
        conversation_id=conversation_id,
        request_key=key,
        title="数据库学习",
        goal="理解事务隔离与并发更新",
        topic="数据库",
        units=[
            {"key": "isolation", "title": "隔离", "goal": "解释隔离级别"},
            {
                "key": "concurrency",
                "title": "并发",
                "goal": "解释丢失更新",
                "dependencies": ["isolation"],
            },
        ],
    )


def test_same_conversation_keeps_separate_tasks_and_does_not_inherit_authority(learning):
    tasks, _, jobs, _ = learning
    first = task_in(learning)
    tasks.confirm(first["id"], expected_revision=1, auto_publish=True)
    first = tasks.select(first["id"])
    before = jobs.messages(first["conversation_id"])
    second = task_in(learning, key="two", conversation_id=first["conversation_id"])
    tasks.select(second["id"])
    assert jobs.messages(first["conversation_id"]) == before
    assert tasks.get(first["id"])["status"] == "paused"
    assert tasks.get(second["id"])["confirmed_requirements_revision"] is None
    assert tasks.get(second["id"])["auto_publish"] is False
    assert tasks.get(first["id"])["units"][0]["title"] == "隔离"


def test_task_idempotency_and_cyclic_units_are_checked(learning):
    tasks, _, _, _ = learning
    first = task_in(learning)
    assert task_in(learning, conversation_id=first["conversation_id"])["id"] == first["id"]
    with pytest.raises(ValueError, match="循环"):
        tasks.update(
            first["id"],
            expected_revision=1,
            units=[
                {"key": "a", "title": "A", "goal": "A", "dependencies": ["b"]},
                {"key": "b", "title": "B", "goal": "B", "dependencies": ["a"]},
            ],
        )
    assert len(tasks.get(first["id"])["units"]) == 2


def test_profile_manual_text_is_immutable_and_memory_revocation_is_live(learning):
    _, profiles, _, memories = learning
    task = task_in(learning)
    global_profile = profiles.get()
    global_profile = profiles.update(
        global_profile["id"], expected_revision=1, content="每次先用直观例子解释。"
    )
    task_profile = profiles.get(task_id=task["id"])
    profiles.update(
        task_profile["id"], expected_revision=1, content="本任务聚焦面试，保留事务执行顺序。"
    )
    memory = memories.create("讲解数据库时给出SQL示例", scope="global", topic="")
    frozen = profiles.freeze(task_id=task["id"], query="数据库事务")
    assert len(frozen["profiles"]) == 2
    assert memory["id"] in {r["id"] for r in frozen["automatic"]}
    profiles.update(global_profile["id"], expected_revision=2, content="新要求下次生效")
    effective = profiles.effective(frozen)
    assert effective["profiles"][0]["content"] == "每次先用直观例子解释。"
    memories.update(memory["id"], expected_revision=1, status="inactive")
    assert profiles.effective(frozen)["automatic"] == []
    assert profiles.get()["content"] == "新要求下次生效"


def test_import_is_reviewed_text_only_and_conflicts_preserve_manual_content(learning):
    _, profiles, _, _ = learning
    profile = profiles.get()
    preview = profiles.preview_import(
        profile["id"],
        expected_revision=1,
        filename="SKILL.md",
        content="---\nname: reader\n---\n请先举例。\n```sh\ninstall anything\n```\n",
    )
    assert profiles.get()["content"] == ""
    assert preview["text_only"] is True
    result = profiles.apply_import(
        profile["id"], preview_id=preview["id"], expected_hash=preview["sha256"]
    )
    assert result["source"]["kind"] == "import"
    assert "install anything" in result["content"]
    assert (
        profiles.apply_import(
            profile["id"], preview_id=preview["id"], expected_hash=preview["sha256"]
        )["revision"]
        == 2
    )
    second = profiles.preview_import(
        profile["id"], expected_revision=2, filename="new.md", content="另一版本"
    )
    profiles.update(profile["id"], expected_revision=2, content="后来手工编辑")
    with pytest.raises(ValueError, match="修订"):
        profiles.apply_import(
            profile["id"], preview_id=second["id"], expected_hash=second["sha256"]
        )
    assert profiles.get()["content"] == "后来手工编辑"
    restored = profiles.rollback(profile["id"], expected_revision=3, target_revision=2)
    assert restored["content"] == result["content"] and restored["revision"] == 4
    exported = profiles.export(profile["id"])
    assert result["content"] in exported


def test_disabled_profile_never_reactivates_a_frozen_run(learning):
    _, profiles, _, _ = learning
    task = task_in(learning)
    global_profile = profiles.get()
    frozen = profiles.freeze(task_id=task["id"], query="数据库")
    profiles.update(global_profile["id"], expected_revision=1, enabled=False)
    profiles.update(global_profile["id"], expected_revision=2, enabled=True)
    assert global_profile["id"] not in {p["id"] for p in profiles.effective(frozen)["profiles"]}
