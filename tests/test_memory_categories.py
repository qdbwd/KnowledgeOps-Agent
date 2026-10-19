"""动态展示类别不改变记忆正文、内部语义、撤回与冻结修订。"""

import importlib

import pytest
from sqlalchemy import select
from test_api_worker import client as _client
from test_api_worker import services as _services

from knowledgeops_agent.memory.service import MemoryService
from knowledgeops_agent.persistence.models import PreferenceRevision, Run

client = _client
services = _services


def categories(sessions):
    return importlib.import_module("knowledgeops_agent.memory.categories").MemoryCategories(
        sessions
    )


def test_manual_category_is_optional_and_does_not_change_internal_semantics(business_sessions):
    service = MemoryService(business_sessions)
    row = service.create("我在学习 Python", scope="global", topic="", display_category_id=None)
    assert row["display_category_id"] is None
    assert row["classification"]["status"] == "unclassified"
    assert row["status"] == "active"
    label = categories(business_sessions).create("学习安排")
    changed = service.update(
        row["id"], expected_revision=row["revision"], display_category_id=label["id"]
    )
    assert changed["display_category_id"] == label["id"]
    for field in (
        "content",
        "category",
        "scope",
        "topic",
        "status",
        "confidence",
        "confirmed_at",
        "review_after",
        "revocation_epoch",
    ):
        assert changed[field] == row[field]
    assert changed["classification"] == {"status": "classified", "source": "user"}
    assert service.history(row["id"])[0]["display_category_id"] is None
    with business_sessions() as session:
        assert not list(session.scalars(select(Run)))


def test_category_rename_delete_preserves_memory_and_old_versions(business_sessions):
    labels = categories(business_sessions)
    label = labels.create("工作情况")
    service = MemoryService(business_sessions)
    memory = service.create(
        "我是 Python 开发者",
        category="background",
        scope="global",
        topic="",
        display_category_id=label["id"],
    )
    with business_sessions() as session:
        old = session.get(PreferenceRevision, (memory["id"], 1)).content.copy()
    renamed = labels.update(label["id"], expected_revision=1, name="职业背景")
    assert renamed["name"] == "职业背景" and renamed["revision"] == 2
    assert service.get(memory["id"])["revision"] == 1
    with pytest.raises(ValueError, match="修订"):
        labels.delete(label["id"], expected_revision=1)
    removed = labels.delete(label["id"], expected_revision=2)
    assert removed["moved_memories"] == 1
    current = service.get(memory["id"])
    assert current["display_category_id"] is None
    assert current["classification"]["status"] == "unclassified"
    assert current["revision"] == 2
    for field in (
        "content",
        "category",
        "status",
        "confirmed_at",
        "review_after",
        "revocation_epoch",
    ):
        assert current[field] == memory[field]
    assert service.history(memory["id"])[0] == old
    assert label["id"] not in {row["id"] for row in labels.list()}
    with pytest.raises(ValueError, match="类别"):
        service.update(memory["id"], expected_revision=2, display_category_id=label["id"])
    assert labels.create("职业背景")["id"] != label["id"]


def test_categories_validate_names_and_do_not_resurrect_deleted_defaults(business_sessions):
    labels = categories(business_sessions)
    seed = next(row for row in labels.list() if row["name"] == "表达偏好")
    labels.delete(seed["id"], expected_revision=seed["revision"])
    service = MemoryService(business_sessions)
    legacy = service.create("以后用中文回答", scope="global", topic="")
    assert legacy["category"] == "preference"
    assert legacy["display_category_id"] is None
    assert seed["id"] not in {row["id"] for row in labels.list()}
    label = labels.create("  Python   学习  ")
    assert label["name"] == "Python 学习"
    with pytest.raises(ValueError, match="已存在"):
        labels.create("ｐｙｔｈｏｎ 学习")
    for name in (" ", "a" * 101, "目标\x00"):
        with pytest.raises(ValueError):
            labels.create(name)


def test_stale_category_edit_cannot_overwrite_manual_memory_changes(business_sessions):
    labels = categories(business_sessions)
    label = labels.create("编程")
    service = MemoryService(business_sessions)
    memory = service.create("先举例", scope="global", topic="", display_category_id=None)
    updated = service.update(memory["id"], expected_revision=1, content="先举 Python 例子")
    with pytest.raises(ValueError, match="修订"):
        service.update(memory["id"], expected_revision=1, display_category_id=label["id"])
    assert service.get(memory["id"])["content"] == updated["content"]
    assert service.get(memory["id"])["display_category_id"] is None


def test_category_http_round_trip_and_legacy_input_without_model(client, services):
    row = client.post("/api/memories", json={"content": "用 Python 举例"}).json()
    assert row["display_category_id"] is None and row["status"] == "active"
    legacy = client.post(
        "/api/memories", json={"content": "我是开发者", "category": "background"}
    ).json()
    assert legacy["display_category_id"] == "memory-category-background"
    category = client.post("/api/memory-categories", json={"name": "我的学习习惯"})
    assert category.status_code == 201
    identity = category.json()["id"]
    changed = client.patch(
        f"/api/memories/{row['id']}",
        json={
            "expected_revision": row["revision"],
            "display_category_id": identity,
        },
    )
    assert changed.status_code == 200
    assert changed.json()["content"] == row["content"]
    result = client.patch(
        f"/api/memory-categories/{identity}",
        json={
            "expected_revision": 1,
            "name": "学习偏好",
        },
    )
    assert result.status_code == 200 and result.json()["revision"] == 2
    assert (
        client.request(
            "DELETE", f"/api/memory-categories/{identity}", json={"expected_revision": 1}
        ).status_code
        == 409
    )
    assert (
        client.request(
            "DELETE", f"/api/memory-categories/{identity}", json={"expected_revision": 2}
        ).json()["moved_memories"]
        == 1
    )
    kept = client.get(f"/api/memories/{row['id']}").json()
    assert kept["content"] == row["content"] and kept["display_category_id"] is None
    assert services.gateway.ledger.records() == []


def test_merge_preserves_shared_display_category_and_undo_preserves_originals(business_sessions):
    service = MemoryService(business_sessions)
    label = categories(business_sessions).create("编程沟通")
    first, second = [
        service.create(text, scope="global", topic="", display_category_id=label["id"])
        for text in ("代码先给 Python 示例", "示例说明输入和输出")
    ]
    merged = service.merge(
        revisions={first["id"]: first["revision"], second["id"]: second["revision"]},
        content="代码先给 Python 示例并说明输入输出",
    )
    assert merged["display_category_id"] == label["id"]
    service.undo_merge(merged["id"], expected_revision=merged["revision"])
    for row in (first, second):
        current = service.get(row["id"])
        assert current["status"] == "active" and current["display_category_id"] == label["id"]


@pytest.mark.parametrize("deleted", [False, True])
def test_rollback_restores_display_choice_without_reviving_deleted_category(
    business_sessions, deleted
):
    service = MemoryService(business_sessions)
    labels = categories(business_sessions)
    original = labels.create("原有类别")
    replacement = labels.create("新类别")
    row = service.create(
        "解释时先给例子", scope="global", topic="", display_category_id=original["id"]
    )
    current = service.update(row["id"], expected_revision=1, display_category_id=replacement["id"])
    if deleted:
        labels.delete(original["id"], expected_revision=1)
    restored = service.rollback(row["id"], expected_revision=current["revision"], target_revision=1)
    assert restored["display_category_id"] == (None if deleted else original["id"])
    assert restored["content"] == row["content"]
    assert service.history(row["id"])[0]["display_category_id"] == original["id"]
