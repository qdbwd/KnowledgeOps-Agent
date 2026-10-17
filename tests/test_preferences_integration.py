"""偏好和授权共用一次解释请求，但权限校验与持久化结果相互独立。"""

import json

from langchain_core.messages import AIMessage
from test_api_worker import client as _client
from test_api_worker import services as _services
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

client = _client
services = _services
runtime_services = _runtime_services


def test_preference_web_lifecycle_requires_revision_and_no_model(client, services):
    assert client.get("/api/preferences").json() == []
    response = client.post(
        "/api/preferences", json={"content": "详细解释", "scope": "global", "topic": ""}
    )
    assert response.status_code == 201
    row = response.json()
    url = f"/api/preferences/{row['id']}"
    assert client.patch(url, json={"expected_revision": 2, "status": "inactive"}).status_code == 409
    changed = client.patch(url, json={"expected_revision": 1, "content": "先讲原理"}).json()
    assert changed["revision"] == 2
    assert client.patch(url, json={"expected_revision": 2, "status": "deleted"}).status_code == 200
    assert client.get("/api/preferences").json() == []
    assert len(client.get(url + "/history").json()) == 3
    assert len(services.gateway.ledger.records()) == 0


def test_single_intent_call_extracts_preference_separately_from_publication(runtime_services):
    from test_preferences import proposal

    from knowledgeops_agent.harness.runtime.resources import prepare_resources
    from knowledgeops_agent.knowledge.intent import ensure_publication_intent
    from knowledgeops_agent.memory.service import PreferenceService

    _, context, run = setup_run(runtime_services, task="以后请先解释机制。")

    class Classifier:
        def invoke(self, messages, **kwargs):
            payload = json.loads(messages[-1].text)
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
                        "preferences": [proposal("先解释机制", run["task"])],
                    }
                )
            )

    context.gateway.client = Classifier()
    prepare_resources(context)
    result = ensure_publication_intent(context)
    service = PreferenceService(context.jobs.sessions)
    assert result["mode"] == "preview"
    assert service.snapshot()[0]["content"] == "先解释机制"
    assert len(context.gateway.ledger.records()) == 1
    assert ensure_publication_intent(context) == result
    assert len(context.gateway.ledger.records()) == 1
