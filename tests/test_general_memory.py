"""通用记忆的真实PG合同；不调用模型，不将模拟读者当用户证据。"""

import importlib
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_preferences import proposal
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.persistence.models import ChatMessage, Preference, utcnow

runtime_services = _runtime_services


def service_for(services):
    return importlib.import_module("knowledgeops_agent.memory.service").MemoryService(
        services[0].sessions
    )


def extract(service, context, run, **values):
    message = context.jobs.publication_context(run["id"])["message"]
    return service.extract(
        message["id"],
        [proposal(values.pop("content", message["content"]), message["content"], **values)],
        known=service.list(),
    )


def test_self_report_and_goal_are_memories_but_generated_messages_are_not(runtime_services):
    service = service_for(runtime_services)
    _, context, run = setup_run(runtime_services, task="我是Python后端开发者，有两年开发经验。")
    result = extract(service, context, run, category="background")
    row = service.get(result["created"][0])
    assert row["status"] == "active" and row["category"] == "background"
    assert row["evidence"][0]["message_id"] == row["source_message_id"]
    assert row["review_after"]
    with runtime_services[0].sessions.begin() as session:
        msg = ChatMessage(
            conversation_id=run["conversation_id"],
            run_id=run["id"],
            role="assistant",
            content="用户已经精通Python",
        )
        session.add(msg)
        session.flush()
        false_source = msg.id
    with pytest.raises(KeyError):
        service.extract(
            false_source,
            [proposal("精通Python", "用户已经精通Python", category="background")],
            known=[],
        )
    assert len(service.list()) == 1


def test_repeated_inferred_style_needs_distinct_real_contexts(runtime_services):
    service = service_for(runtime_services)
    for index in range(2):
        _, context, run = setup_run(runtime_services, task="我希望解释时多给具体例子。")
        result = extract(
            service,
            context,
            run,
            content="讲解时多给具体例子",
            category="preference",
            explicit=False,
        )
        rows = service.list()
        assert len(rows) == 1
        assert rows[0]["status"] == ("pending" if index == 0 else "active")
        # 同一条真人消息重放不会增加证据或更改已提取的判断。
        assert (
            extract(service, context, run, content="讲解时多给具体例子", explicit=False) == result
        )
    assert len(service.get(rows[0]["id"])["evidence"]) == 2


def test_same_conversation_repetition_does_not_establish_cross_context_habit(runtime_services):
    service = service_for(runtime_services)
    _, context, run = setup_run(runtime_services, task="我希望解释时多给例子。")
    extract(service, context, run, content="多给例子", explicit=False)
    queued = context.jobs.enqueue(
        run["conversation_id"], run["task"], request_key="second-real-message"
    )
    extract(service, context, queued, content="多给例子", explicit=False)
    assert service.list()[0]["status"] == "pending"


def test_scoped_chinese_bm25_retrieval_and_frozen_revision(runtime_services):
    service = service_for(runtime_services)
    relevant = service.create(
        "我正在学习PostgreSQL事务隔离与索引", category="goal", scope="topic", topic="数据库"
    )
    other = service.create(
        "我正在学习PostgreSQL财务系统", category="goal", scope="topic", topic="财务"
    )
    service.create("我喜欢烘焙面包", category="background", scope="global", topic="")
    frozen = service.freeze("PostgreSQL事务隔离", topic="数据库")
    ids = {r["id"] for r in frozen["selected"]}
    assert relevant["id"] in ids and other["id"] not in ids
    assert frozen["token_count"] <= 2000
    service.update(relevant["id"], expected_revision=1, content="我正在学习烘焙")
    found = service.search("PostgreSQL事务隔离", topic="数据库", catalog=frozen["catalog"])
    assert found[0]["revision"] == 1 and "事务隔离" in found[0]["content"]
    assert service.search("PostgreSQL事务隔离", topic="数据库") == []
    service.update(relevant["id"], expected_revision=2, status="inactive")
    assert service.effective(frozen["selected"]) == []
    service.update(relevant["id"], expected_revision=3, status="active")
    assert service.search("PostgreSQL事务隔离", topic="数据库", catalog=frozen["catalog"]) == []


def test_manual_versions_rollback_and_evidence_not_overwritten_by_inference(runtime_services):
    service = service_for(runtime_services)
    original = service.create("解释先讲原理", scope="global", topic="")
    revised = service.update(original["id"], expected_revision=1, content="先给例子")
    restored = service.rollback(
        original["id"], expected_revision=revised["revision"], target_revision=1
    )
    assert restored["revision"] == 3 and restored["content"] == original["content"]
    _, context, run = setup_run(runtime_services, task="我希望先给简短答案。")
    result = extract(
        service, context, run, content="先给简短答案", target_id=original["id"], explicit=False
    )
    assert result["created"] and service.get(original["id"])["status"] == "active"


def test_injection_budget_counts_metadata_corrections_and_aged_background(runtime_services):
    service = service_for(runtime_services)
    correction = service.create(
        "关于Python我的基础是初学，不是高级工程师。",
        category="correction",
        scope="global",
        topic="",
    )
    background = service.create("我是Python开发者", category="background", scope="global", topic="")
    for i in range(18):
        service.create(
            f"我学习Python时偏好例子{i}：" + "解释示例与运行原理。" * 120, scope="global", topic=""
        )
    with runtime_services[0].sessions.begin() as session:
        row = session.get(Preference, background["id"])
        row.review_after = utcnow() - timedelta(days=1)
    snapshot = service.freeze("Python学习", token_budget=2000, correction_budget=500)
    assert correction["id"] in {r["id"] for r in snapshot["selected"]}
    assert snapshot["token_count"] <= 2000
    assert service.get(background["id"])["needs_review"]
    assert service.get(correction["id"])["review_after"] is None
    assert len(snapshot["selected"]) < len(service.list())


def test_index_rebuild_preserves_identity_history_and_removal(runtime_services):
    service = service_for(runtime_services)
    row = service.create("Python基础解释", scope="global", topic="")
    service.update(row["id"], expected_revision=1, content="SQL数据库解释")
    before = service.history(row["id"])
    report = service.rebuild_index()
    assert report["indexed_revisions"] == 2
    assert service.history(row["id"]) == before
    assert service.search("SQL数据库")[0]["id"] == row["id"]
    service.update(row["id"], expected_revision=2, status="deleted")
    assert service.search("SQL数据库") == []
    with runtime_services[0].sessions() as session:
        assert session.scalar(select(Preference).where(Preference.id == row["id"])) is not None


def test_semantic_merge_is_explicit_preserves_sources_and_can_be_undone(runtime_services):
    service = service_for(runtime_services)
    first = service.create("解释先给例子", scope="global", topic="")
    second = service.create("解释需要完整背景", scope="global", topic="")
    original = service.freeze("解释")
    merged = service.merge(
        revisions={first["id"]: 1, second["id"]: 1}, content="先给例子再讲完整背景"
    )
    assert {r["id"] for r in service.snapshot()} == {merged["id"]}
    assert len(merged["source"]["merged_from"]) == 2
    assert service.history(first["id"])[0]["content"] == "解释先给例子"
    service.undo_merge(merged["id"], expected_revision=1)
    assert {r["id"] for r in service.snapshot()} == {first["id"], second["id"]}
    # 人工撤销不重新授权此前已经撤回的运行快照。
    assert service.effective(original["selected"]) == []


def test_merge_rollback_refuses_to_overwrite_later_user_edits(runtime_services):
    service = service_for(runtime_services)
    first = service.create("讲解详细", scope="global", topic="")
    second = service.create("讲解连贯", scope="global", topic="")
    merged = service.merge(revisions={first["id"]: 1, second["id"]: 1}, content="讲解详细连贯")
    service.update(first["id"], expected_revision=2, content="后来改为简短")
    with pytest.raises(ValueError, match="另有修改"):
        service.undo_merge(merged["id"], expected_revision=1)
    assert service.get(merged["id"])["status"] == "active"
    assert service.get(first["id"])["content"] == "后来改为简短"
