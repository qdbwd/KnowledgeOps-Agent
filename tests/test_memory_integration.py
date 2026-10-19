"""长期记忆通过既有意图/请求/工具管理路径使用；所有供应商响应为替身。"""

import json

import pytest
from langchain_core.messages import AIMessage
from test_api_worker import client as _client
from test_api_worker import services as _services
from test_preferences import proposal
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.harness.runtime.resources import prepare_resources
from knowledgeops_agent.knowledge.intent import ensure_publication_intent
from knowledgeops_agent.memory.service import MemoryService

client = _client
services = _services
runtime_services = _runtime_services


def test_api_unifies_legacy_preferences_and_general_memory(client, services):
    first = client.post("/api/preferences", json={"content": "解释先给例子"}).json()
    background = client.post(
        "/api/memories", json={"content": "我是Python开发者", "category": "background"}
    ).json()
    assert {r["id"] for r in client.get("/api/memories").json()} == {first["id"], background["id"]}
    assert [r["id"] for r in client.get("/api/preferences").json()] == [first["id"]]
    url = "/api/memories/" + background["id"]
    assert client.get(url).json()["evidence"][0]["message_id"] is None
    assert (
        client.patch(url, json={"expected_revision": 1, "content": "我是数据库开发者"}).status_code
        == 200
    )
    assert client.patch(url, json={"expected_revision": 1, "status": "deleted"}).status_code == 409
    assert (
        client.post(
            url + "/rollback", json={"expected_revision": 2, "target_revision": 1}
        ).status_code
        == 200
    )
    assert (
        client.get("/api/memories/search", params={"query": "Python"}).json()[0]["id"]
        == background["id"]
    )
    assert len(client.get(url + "/history").json()) == 3
    assert not services.gateway.ledger.records()


def test_real_intent_bridge_extracts_background_with_one_mock_model_call(runtime_services):
    _, context, run = setup_run(runtime_services, task="我是Python后端开发者，有两年开发经验。")

    class Interpreter:
        def invoke(self, messages, **kwargs):
            payload = json.loads(messages[-1].text)
            assert "background" in messages[0].text
            return AIMessage(
                json.dumps(
                    {
                        "action": "none",
                        "clear": False,
                        "message_id": payload["message"]["id"],
                        "root_id": run["id"],
                        "quote": "",
                        "changeset_id": None,
                        "expected_revision": None,
                        "confirm_impact": False,
                        "ui_conflict": False,
                        "continue_task": True,
                        "preferences": [
                            proposal(
                                "用户自述两年Python后端经验", run["task"], category="background"
                            )
                        ],
                    },
                    ensure_ascii=False,
                )
            )

    context.gateway.client = Interpreter()
    prepare_resources(context)
    result = ensure_publication_intent(context)
    assert result["preferences"]["created"]
    assert MemoryService(context.jobs.sessions).list()[0]["category"] == "background"
    assert len(context.gateway.ledger.records()) == 1
    assert ensure_publication_intent(context) == result
    assert len(context.gateway.ledger.records()) == 1


def test_chat_extraction_classifies_in_same_request_and_replay_does_not_duplicate(runtime_services):
    from knowledgeops_agent.memory.categories import MemoryCategories

    _, context, run = setup_run(runtime_services, task="我是 Python 后端开发者。")
    calls = []

    class Interpreter:
        def invoke(self, messages, **kwargs):
            payload = json.loads(messages[-1].text)
            calls.append(payload)
            assert payload["memory_categories"]
            assert "display_category" in messages[0].text
            return AIMessage(
                json.dumps(
                    {
                        "action": "none",
                        "clear": False,
                        "message_id": payload["message"]["id"],
                        "root_id": run["id"],
                        "quote": "",
                        "changeset_id": None,
                        "expected_revision": None,
                        "confirm_impact": False,
                        "ui_conflict": False,
                        "continue_task": True,
                        "preferences": [
                            proposal(
                                "Python 后端开发者",
                                run["task"],
                                category="background",
                                display_category={"category_id": None, "new_category": "编程经历"},
                            )
                        ],
                    },
                    ensure_ascii=False,
                )
            )

    context.gateway.client = Interpreter()
    prepare_resources(context)
    result = ensure_publication_intent(context)
    memory = MemoryService(context.jobs.sessions).get(result["preferences"]["created"][0])
    label = next(
        row for row in MemoryCategories(context.jobs.sessions).list() if row["name"] == "编程经历"
    )
    assert memory["display_category_id"] == label["id"]
    assert memory["category"] == "background" and memory["review_after"]
    assert memory["classification"]["source"] == "extraction"
    assert ensure_publication_intent(context) == result
    assert len(calls) == len(context.gateway.ledger.records()) == 1


def test_new_resource_snapshot_selects_bounded_memory_and_keeps_same_snapshot(runtime_services):
    _, context, _ = setup_run(runtime_services, task="讲讲Python并发")
    service = MemoryService(context.jobs.sessions)
    relevant = service.create("我一直在学习Python并发", category="goal", scope="global", topic="")
    irrelevant = service.create("我喜欢烘焙面包", category="background", scope="global", topic="")
    manifest = prepare_resources(context)
    assert relevant["id"] in {r["id"] for r in manifest["preferences"]}
    assert irrelevant["id"] not in {r["id"] for r in manifest["preferences"]}
    assert manifest["memory"]["token_count"] <= 2000
    service.update(relevant["id"], expected_revision=1, content="我的当前目标是SQL事务")
    assert prepare_resources(context) == manifest
    assert service.read(relevant["id"], catalog=manifest["memory"]["catalog"])["revision"] == 1


@pytest.mark.parametrize("choice", ["invalid", "deleted", "reuse"])
def test_extracted_display_choice_cannot_lose_verified_text_or_resurrect_removed_labels(
    runtime_services, choice
):
    from knowledgeops_agent.memory.categories import MemoryCategories, model_catalog

    jobs = runtime_services[0]
    labels = MemoryCategories(jobs.sessions)
    label = labels.create("开发经历")
    _, context, run = setup_run(runtime_services, task="我是 Python 开发者。")
    with jobs.sessions() as session:
        frozen = model_catalog(session)
    if choice == "deleted":
        labels.delete(label["id"], expected_revision=1)
    selected = {
        "category_id": "missing" if choice == "invalid" else label["id"],
        "new_category": None,
    }
    message = jobs.publication_context(run["id"])["message"]
    service = MemoryService(jobs.sessions)
    result = service.extract(
        message["id"],
        [
            proposal(
                "Python 开发者",
                message["content"],
                category="background",
                display_category=selected,
            )
        ],
        known=[],
        known_categories=frozen,
    )
    memory = service.get(result["created"][0])
    assert memory["content"] == "Python 开发者" and memory["status"] == "active"
    assert memory["display_category_id"] == (label["id"] if choice == "reuse" else None)
    assert memory["category"] == "background" and memory["review_after"]
    if choice == "deleted":
        assert label["id"] not in {row["id"] for row in labels.list()}
    assert not context.gateway.ledger.records()


def test_actual_graph_can_search_and_reread_frozen_memory(runtime_services):
    from types import SimpleNamespace

    from fixtures.learning_provider import chunk
    from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
    from langgraph.checkpoint.memory import InMemorySaver

    from knowledgeops_agent.harness.tools.policy import ToolPolicyService

    module, context, run = setup_run(runtime_services, task="核对我学习Python的背景")
    ToolPolicyService(context.jobs.sessions).update(
        expected_revision=0, enabled={"builtin:memory_search": True, "builtin:memory_read": True}
    )
    memory = MemoryService(context.jobs.sessions).create(
        "我正在学习Python异步编程", category="goal", scope="global", topic=""
    )
    captured = []

    class Reader:
        def bind_tools(self, tools):
            return SimpleNamespace(stream=self.stream)

        def stream(self, messages, **kwargs):
            observations = [
                m
                for m in messages
                if isinstance(m, ToolMessage) and m.name in {"memory_search", "memory_read"}
            ]
            if not observations:
                yield chunk("memory_search", {"query": "Python异步编程"}, "memory-search")
            elif observations[-1].name == "memory_search":
                data = json.loads(observations[-1].content)
                assert data["results"][0]["id"] == memory["id"]
                yield chunk("memory_read", {"memory_id": memory["id"]}, "memory-read")
            else:
                captured.append(json.loads(observations[-1].content))
                yield AIMessageChunk(
                    content="已核对用户保存的Python学习目标；不能据此推断掌握程度。"
                )

    context.gateway.client = Reader()
    module.build_agent(context, checkpointer=InMemorySaver()).invoke(
        {"messages": [HumanMessage(run["task"])]},
        {"configurable": {"thread_id": run["checkpoint_id"]}},
        context=context,
    )
    assert captured and captured[0]["id"] == memory["id"] and captured[0]["revision"] == 1
