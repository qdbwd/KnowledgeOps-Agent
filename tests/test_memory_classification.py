"""单次分类经真实Run/outbox/网关执行；供应商替身不证明语义质量。"""

import importlib
import json

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select
from test_api_worker import client as _client
from test_api_worker import services as _services

from knowledgeops_agent.memory.categories import MemoryCategories
from knowledgeops_agent.memory.service import MemoryService
from knowledgeops_agent.persistence.models import ChatMessage, Run, RunDispatch

client = _client
services = _services


class Classifier:
    def __init__(self, response=None, callback=None):
        self.response = response or {"category_id": None, "new_category": "编程学习"}
        self.callback = callback
        self.calls = 0

    def invoke(self, messages, **kwargs):
        self.calls += 1
        payload = json.loads(messages[-1].text)
        assert payload["content"] and payload["categories"]
        assert not kwargs.get("tools")
        if self.callback:
            self.callback()
        return AIMessage(json.dumps(self.response, ensure_ascii=False))


def consume(services, identity):
    from knowledgeops_agent.app.runs.worker import execute_run

    row = services.jobs.get_run(identity)
    return execute_run(identity, row["generation"], services=services, worker_id="classifier-test")


def test_manual_save_then_single_bounded_classification_and_duplicate_delivery(client, services):
    provider = Classifier()
    services.gateway.enabled, services.gateway.client = True, provider
    response = client.post("/api/memories", json={"content": "我在学 Python"})
    assert response.status_code == 201
    original = response.json()
    assert original["content"] == "我在学 Python" and original["status"] == "active"
    assert original["classification"]["status"] == "pending"
    identity = original["classification"]["run_id"]
    assert provider.calls == 0
    with services.sessions() as session:
        run = session.get(Run, identity)
        assert run.kind == "memory_classification" and run.budgets["model_requests"] == 1
        assert run.conversation_id is None
        assert session.scalar(select(RunDispatch).where(RunDispatch.run_id == identity))
        assert not list(session.scalars(select(ChatMessage)))
    finished = consume(services, identity)
    assert finished["status"] == "completed"
    memory = client.get(f"/api/memories/{original['id']}").json()
    assert memory["classification"]["status"] == "classified"
    assert memory["revision"] == original["revision"] + 1
    for field in (
        "content",
        "category",
        "scope",
        "topic",
        "status",
        "revocation_epoch",
        "confirmed_at",
        "review_after",
    ):
        assert memory[field] == original[field]
    assert any(
        row["id"] == memory["display_category_id"] and row["name"] == "编程学习"
        for row in client.get("/api/memory-categories").json()
    )
    assert consume(services, identity) is None
    assert provider.calls == 1
    assert len(services.gateway.ledger.records(root_id=identity)) == 1


def test_model_disabled_keeps_saved_text_and_can_later_retry(client, services):
    original = client.post("/api/memories", json={"content": "保留这段背景"}).json()
    assert original["classification"] == {
        "status": "unclassified",
        "source": "model",
        "error": "model_disabled",
    }
    with services.sessions() as session:
        assert not list(session.scalars(select(Run)))
    services.gateway.enabled, services.gateway.client = True, Classifier()
    payload = {"expected_revision": original["revision"], "request_key": "retry-after-enable"}
    response = client.post(f"/api/memories/{original['id']}/classify", json=payload)
    assert response.status_code == 202
    again = client.post(f"/api/memories/{original['id']}/classify", json=payload).json()
    assert again["classification"]["run_id"] == response.json()["classification"]["run_id"]
    assert consume(services, again["classification"]["run_id"])["status"] == "completed"


@pytest.mark.parametrize("change", ["content", "label", "catalog", "delete"])
def test_changes_during_request_cannot_be_overwritten(client, services, change):
    services.gateway.enabled, services.gateway.client = True, Classifier()
    original = client.post("/api/memories", json={"content": "我在学 Python"}).json()
    memory = MemoryService(services.sessions)
    labels = MemoryCategories(services.sessions)
    target = next(row for row in labels.list() if row["name"] == "用户背景")

    def modify():
        if change == "catalog":
            labels.update(target["id"], expected_revision=target["revision"], name="新背景")
        else:
            patch = (
                {"content": "我是 Rust 开发者"}
                if change == "content"
                else (
                    {"display_category_id": target["id"]}
                    if change == "label"
                    else {"status": "deleted"}
                )
            )
            memory.update(original["id"], expected_revision=original["revision"], **patch)

    services.gateway.client.callback = modify
    finished = consume(services, original["classification"]["run_id"])
    assert finished["outcome"]["kind"] in {
        "memory_classification_skipped",
        "memory_classification_failed",
    }
    current = memory.get(original["id"])
    assert current["display_category_id"] == (target["id"] if change == "label" else None)
    assert not any(row["name"] == "编程学习" for row in labels.list())
    if change == "content":
        assert current["content"] == "我是 Rust 开发者"
    if change == "delete":
        assert current["status"] == "deleted"


def test_invalid_output_is_not_retried_and_explicit_retry_uses_new_run(client, services):
    provider = Classifier({"category_id": "unknown-category", "new_category": None})
    services.gateway.enabled, services.gateway.client = True, provider
    memory = client.post("/api/memories", json={"content": "我在学 Python"}).json()
    identity = memory["classification"]["run_id"]
    assert consume(services, identity)["status"] == "failed"
    current = client.get(f"/api/memories/{memory['id']}").json()
    assert current["content"] == memory["content"] and current["display_category_id"] is None
    assert current["classification"]["status"] == "failed"
    assert consume(services, identity) is None and provider.calls == 1
    provider.response = {"category_id": "memory-category-goal", "new_category": None}
    retried = client.post(
        f"/api/memories/{memory['id']}/classify",
        json={
            "expected_revision": current["revision"],
            "request_key": "explicit-retry",
        },
    ).json()
    next_id = retried["classification"]["run_id"]
    assert next_id != identity
    assert consume(services, next_id)["status"] == "completed"
    assert provider.calls == 2


def test_cancellation_during_response_preserves_text_without_applying_category(client, services):
    provider = Classifier()
    services.gateway.enabled, services.gateway.client = True, provider
    memory = client.post("/api/memories", json={"content": "只保存我的原文"}).json()
    identity = memory["classification"]["run_id"]
    provider.callback = lambda: services.jobs.cancel(identity)
    assert consume(services, identity)["status"] == "cancelled"
    current = client.get(f"/api/memories/{memory['id']}").json()
    assert current["content"] == memory["content"] and current["display_category_id"] is None
    assert current["classification"]["status"] != "pending"
    assert provider.calls == 1


def test_save_and_outbox_roll_back_together_on_enqueue_failure(client, services, monkeypatch):
    module = importlib.import_module("knowledgeops_agent.memory.classification")
    services.gateway.enabled, services.gateway.client = True, Classifier()

    def fail(*args, **kwargs):
        raise ValueError("queue transaction intentionally failed")

    monkeypatch.setattr(module, "queue_classification", fail)
    response = client.post("/api/memories", json={"content": "不留下半个保存请求"})
    assert response.status_code >= 400
    assert client.get("/api/memories").json() == []


def test_pause_after_provider_response_resumes_from_receipt_without_another_request(
    client, services
):
    provider = Classifier()
    services.gateway.enabled, services.gateway.client = True, provider
    memory = client.post("/api/memories", json={"content": "我在学异步程序"}).json()
    identity = memory["classification"]["run_id"]
    provider.callback = lambda: services.jobs.pause(identity)
    assert consume(services, identity)["status"] == "paused"
    paused = client.get(f"/api/memories/{memory['id']}").json()
    assert paused["classification"]["status"] == "paused"
    assert paused["display_category_id"] is None
    assert (
        client.post(
            f"/api/memories/{memory['id']}/classify",
            json={
                "expected_revision": paused["revision"],
                "request_key": "cannot-bypass-paused",
            },
        ).status_code
        == 409
    )
    provider.callback = None
    with pytest.raises(ValueError, match="不能修改"):
        services.jobs.resume(identity, budgets={"model_requests": 10})
    services.jobs.resume(identity)
    assert consume(services, identity)["status"] == "completed"
    assert provider.calls == 1


def test_crash_after_category_commit_recovers_without_duplicate_revision(
    client, services, monkeypatch
):
    provider = Classifier()
    services.gateway.enabled, services.gateway.client = True, provider
    memory = client.post("/api/memories", json={"content": "我在学恢复机制"}).json()
    identity = memory["classification"]["run_id"]
    original_finish = services.jobs.finish

    def crash(*args, **kwargs):
        raise RuntimeError("simulated crash after memory transaction")

    monkeypatch.setattr(services.jobs, "finish", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        consume(services, identity)
    committed = client.get(f"/api/memories/{memory['id']}").json()
    assert committed["classification"]["status"] == "classified"
    monkeypatch.setattr(services.jobs, "finish", original_finish)
    assert services.jobs.recover_on_startup() >= 1
    assert services.jobs.get_run(identity)["status"] == "interrupted"
    assert consume(services, identity) is None
    services.jobs.resume(identity)
    assert consume(services, identity)["status"] == "completed"
    assert client.get(f"/api/memories/{memory['id']}").json()["revision"] == committed["revision"]
    assert provider.calls == 1


def test_unknown_provider_outcome_is_not_automatically_repeated(client, services):
    def crash():
        raise SystemExit("simulated process exit with pending model receipt")

    provider = Classifier(callback=crash)
    services.gateway.enabled, services.gateway.client = True, provider
    memory = client.post("/api/memories", json={"content": "保留未知结果时的原文"}).json()
    identity = memory["classification"]["run_id"]
    with pytest.raises(SystemExit):
        consume(services, identity)
    services.jobs.recover_on_startup()
    provider.callback = None
    services.jobs.resume(identity)
    finished = consume(services, identity)
    assert finished["status"] == "failed"
    current = client.get(f"/api/memories/{memory['id']}").json()
    assert current["content"] == memory["content"] and current["display_category_id"] is None
    assert provider.calls == 1
    assert len(services.gateway.ledger.records(root_id=identity)) == 1
