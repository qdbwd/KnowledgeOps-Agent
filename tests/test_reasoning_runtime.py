"""思考选择从 HTTP 到运行快照及真实图请求的闭环；供应商用替身。"""

from dataclasses import replace
from uuid import uuid4

from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from test_api_worker import client as _client
from test_api_worker import services as _services
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.core.config import Settings
from knowledgeops_agent.harness.models.reasoning import ReasoningService
from knowledgeops_agent.harness.runtime.resources import ResourceService
from knowledgeops_agent.persistence.models import Run

client = _client
services = _services
runtime_services = _runtime_services


def offline_reasoning():
    return ReasoningService(
        Settings(
            _env_file=None,
            model_calls_enabled=False,
            openai_base_url="https://api.deepseek.com",
            agent_model="deepseek-flash",
        )
    )


def test_http_rejects_invalid_choice_and_freezes_default_on_idempotent_retry(client, services):
    services.reasoning = offline_reasoning()
    conversation = client.post("/api/conversations", json={"title": "思考设置"}).json()
    url = f"/api/conversations/{conversation['id']}/messages"
    body = {"content": "读取材料", "request_key": str(uuid4()), "reasoning_effort": "medium"}
    assert client.post(url, json=body).status_code == 409
    assert services.jobs.messages(conversation["id"]) == []
    body["reasoning_effort"] = "default"
    first = client.post(url, json=body)
    assert first.status_code == 202
    assert first.json()["input"]["reasoning"]["effective"] == "high"
    services.reasoning._cached = {**services.reasoning.catalog(), "default_level": "low"}
    retried = client.post(url, json=body)
    assert retried.status_code == 202
    assert retried.json()["id"] == first.json()["id"]
    assert retried.json()["input"]["reasoning"]["effective"] == "high"
    body["reasoning_effort"] = "low"
    assert client.post(url, json=body).status_code == 409


def test_graph_request_resume_and_child_use_original_reasoning(runtime_services):
    module, context, run = setup_run(runtime_services)
    frozen = offline_reasoning().freeze("max")
    with context.jobs.sessions.begin() as session:
        row = session.get(Run, run["id"])
        row.input_data = {**row.input_data, "reasoning": frozen}
    calls = []

    class Provider:
        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            calls.append(kwargs)
            yield AIMessageChunk(content="这是离线工程验证。")

    context.gateway.client = Provider()
    agent = module.build_agent(context, checkpointer=InMemorySaver())
    agent.invoke(
        {"messages": [HumanMessage("说明材料")], "task": "说明材料"},
        {"configurable": {"thread_id": run["id"]}},
        context=context,
    )
    assert calls and all(call["reasoning_effort"] == "max" for call in calls)
    assert all(call["extra_body"] == {"thinking": {"type": "enabled"}} for call in calls)
    service = ResourceService(context.jobs.sessions)
    assert service.ensure(context)["model"]["reasoning"] == frozen
    child = context.jobs.start_child(context.run_id, "读取资料", operation_key="reasoning-child")
    child_context = replace(context, run_id=child["id"])
    assert service.ensure(child_context)["model"]["reasoning"] == frozen
    # 根输入被改也不能替换已保存的请求快照。
    with context.jobs.sessions.begin() as session:
        row = session.scalar(select(Run).where(Run.id == run["id"]))
        row.input_data = {**row.input_data, "reasoning": offline_reasoning().freeze("low")}
    assert service.ensure(context)["model"]["reasoning"] == frozen


def test_legacy_request_retry_does_not_add_new_reasoning_settings(services, client):
    services.reasoning = offline_reasoning()
    conversation = services.jobs.create_conversation()
    key = str(uuid4())
    old = services.jobs.enqueue(conversation["id"], "旧请求重试", request_key=key)
    response = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={
            "content": "旧请求重试",
            "request_key": key,
        },
    )
    assert response.status_code == 202 and response.json()["id"] == old["id"]
    assert "reasoning" not in response.json()["input"]
