"""真实协议与图中断：续接状态持久化、回答验证和多轮重入不重复首轮。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fixtures.mcp_input_provider import InputProvider
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from test_api_worker import client as _client
from test_api_worker import enqueue
from test_api_worker import services as _services

from knowledgeops_agent.app.runs.worker import execute_run
from knowledgeops_agent.harness.mcp.registry import MCPRegistry
from knowledgeops_agent.persistence.models import MCPCall, MCPContinuation

client = _client
services = _services


@pytest.fixture(autouse=True)
def release_test_connections(services):
    yield
    services.jobs.close_mcp_sessions()


def configure(services, path, *, url_mode=False, durable_server=True, protocol_mode="auto", tool_name="collect"):
    registry = MCPRegistry(services.sessions)
    args = [str(Path("tests/fixtures/mcp_input.py").resolve())]
    if durable_server:
        key_path = path.parent / "request-state.key"
        key_path.write_bytes(os.urandom(32))
        args.append(str(key_path))
    binding = registry.create("输入续接验证", {
        "transport": "stdio", "command": sys.executable,
        "args": args,
        "protocol_mode": protocol_mode,
    })
    binding = registry.discover(binding["id"], expected_revision=1)
    registry.update(binding["id"], expected_revision=binding["revision"], enabled=True,
                    permissions={tool_name: "read"})
    provider = InputProvider(path, url_mode=url_mode)
    services.gateway.enabled, services.gateway.client = True, provider
    return provider


def test_two_round_mcp_input_survives_connection_close_without_repeating_first_request(client, services, tmp_path):
    path = tmp_path / "protocol.log"
    provider = configure(services, path)
    _, root, _ = enqueue(client)
    saver = InMemorySaver()
    first = execute_run(root["id"], 1, services=services, checkpointer=saver, worker_id="input-worker")
    assert first["status"] == "waiting_input", first
    assert first["question_count"] == 1
    assert path.read_text().splitlines() == ["first"]
    assert provider.requests == 1
    questions_url = f"/api/runs/{root['id']}/questions"
    first_question = client.get(questions_url).json()[0]
    assert first_question["kind"] == "mcp" and first_question["status"] == "pending"
    assert "opaque:" not in json.dumps(first_question)
    answer_url = f"{questions_url}/{first_question['id']}/answer"
    invalid = {"expected_revision": 1, "kind": "mcp", "value": {
        "quantity": {"action": "accept", "content": {"count": "not-an-integer"}},
    }}
    assert client.post(answer_url, json=invalid).status_code == 409
    assert services.jobs.get_run(root["id"])["status"] == "waiting_input"
    valid = {**invalid, "value": {"quantity": {"action": "accept", "content": {"count": 2}}}}
    assert client.post(answer_url, json=valid).status_code == 200
    generation = services.jobs.get_run(root["id"])["generation"]
    assert client.post(answer_url, json=valid).status_code == 200
    assert services.jobs.get_run(root["id"])["generation"] == generation
    services.jobs.mcp_sessions.close_scope(f"conversation:{root['conversation_id']}:main")
    second = execute_run(root["id"], generation, services=services, checkpointer=saver, worker_id="input-worker-2")
    if second["status"] != "waiting_input":
        with services.sessions() as session:
            receipts = [(row.status, row.result) for row in session.scalars(select(MCPCall).where(MCPCall.run_id == root["id"]))]
        snapshot = saver.get_tuple({"configurable": {"thread_id": root["checkpoint_id"]}})
        messages = snapshot.checkpoint["channel_values"]["messages"] if snapshot else []
        raise AssertionError((second["status"], receipts, [(message.type, message.content) for message in messages if isinstance(message, ToolMessage)]))
    questions = client.get(questions_url).json()
    assert len(questions) == 2 and questions[0]["status"] == "answered"
    second_question = questions[1]
    assert second_question["status"] == "pending"
    assert path.read_text().splitlines() == ["first", "quantity:accept"]
    assert provider.requests == 1
    assert client.post(f"{questions_url}/{second_question['id']}/answer", json={
        "expected_revision": 1, "kind": "mcp", "value": {
            "detail": {"action": "accept", "content": {"depth": "详细"}},
        },
    }).status_code == 200
    generation = services.jobs.get_run(root["id"])["generation"]
    services.jobs.mcp_sessions.close_scope(f"conversation:{root['conversation_id']}:main")
    result = execute_run(root["id"], generation, services=services, checkpointer=saver, worker_id="input-worker-3")
    assert result["status"] == "completed", result
    assert provider.requests == 2 and result["tool_calls"] == 1
    assert "opaque:" not in str(provider.messages)
    assert path.read_text().splitlines() == ["first", "quantity:accept", "detail:accept"]
    with services.sessions() as session:
        receipt = session.scalar(select(MCPCall).where(MCPCall.run_id == root["id"]))
        assert receipt.status == "succeeded"
        rounds = list(session.scalars(select(MCPContinuation).where(MCPContinuation.mcp_call_id == receipt.id)))
        assert len(rounds) == 2 and all(row.status == "completed" for row in rounds)
        exposed = str(provider.messages) + json.dumps(client.get(questions_url).json()) + str(services.jobs.events(root["id"]))
        assert all(row.request_state["state"] not in exposed for row in rounds)


def test_mcp_input_survives_a_new_worker_process_and_pg_checkpoint(client, services, tmp_path):
    path = tmp_path / "protocol.log"
    configure(services, path)
    _, root, _ = enqueue(client)
    with services.engine.connect() as connection:
        schema = connection.exec_driver_sql("SELECT current_schema()").scalar_one()

    def launch(generation):
        result = subprocess.run([
            sys.executable, "tests/reliability_process.py", schema, services.gateway.ledger.scope,
            str(tmp_path), root["id"], str(generation), "mcp",
        ], capture_output=True, text=True, timeout=150)
        assert result.returncode == 0, result.stderr[-4000:]
        return json.loads(result.stdout)

    first = launch(1)
    assert first["run"]["status"] == "waiting_input" and first["model_requests"] == 1
    questions = client.get(f"/api/runs/{root['id']}/questions").json()
    response = client.post(f"/api/runs/{root['id']}/questions/{questions[0]['id']}/answer", json={
        "expected_revision": 1, "kind": "decline", "value": "",
    })
    assert response.status_code == 200
    second = launch(services.jobs.get_run(root["id"])["generation"])
    assert second["run"]["status"] == "completed" and second["model_requests"] == 1
    assert second["run"]["tool_calls"] == 1
    assert path.read_text().splitlines() == ["first", "quantity:decline"]


def test_service_losing_continuation_key_stops_without_replaying_first_request(client, services, tmp_path):
    path = tmp_path / "protocol.log"
    configure(services, path, durable_server=False)
    _, root, _ = enqueue(client)
    saver = InMemorySaver()
    assert execute_run(root["id"], 1, services=services, checkpointer=saver)["status"] == "waiting_input"
    question = client.get(f"/api/runs/{root['id']}/questions").json()[0]
    assert client.post(f"/api/runs/{root['id']}/questions/{question['id']}/answer", json={
        "expected_revision": 1, "kind": "decline", "value": "",
    }).status_code == 200
    services.jobs.mcp_sessions.close_scope(f"conversation:{root['conversation_id']}:main")
    generation = services.jobs.get_run(root["id"])["generation"]
    execute_run(root["id"], generation, services=services, checkpointer=saver)
    with services.sessions() as session:
        receipt = session.scalar(select(MCPCall).where(MCPCall.run_id == root["id"]))
        assert receipt.status == "unknown" and "拒绝了原续接状态" in receipt.result["error"]
    assert path.read_text().splitlines() == ["first"]


def test_legacy_reverse_elicitation_returns_explicit_limitation_without_a_fake_answer(client, services, tmp_path):
    path = tmp_path / "protocol.log"
    configure(services, path, protocol_mode="legacy", tool_name="live_collect")
    _, root, _ = enqueue(client)
    execute_run(root["id"], 1, services=services, checkpointer=InMemorySaver())
    with services.sessions() as session:
        receipt = session.scalar(select(MCPCall).where(MCPCall.run_id == root["id"]))
        assert receipt.status == "failed", receipt.result
        assert "旧式依赖活连接" in receipt.result["preview"]
    assert client.get(f"/api/runs/{root['id']}/questions").json() == []
    assert path.read_text().splitlines() == ["legacy_first"]


@pytest.mark.parametrize("paused", [False, True])
def test_child_mcp_answer_resumes_actual_child_and_respects_group_pause(client, services, tmp_path, paused):
    from test_subagent_api import definition

    from knowledgeops_agent.app.runs.children import ChildRuns
    from knowledgeops_agent.harness.runtime.resources import prepare_resources
    from knowledgeops_agent.harness.runtime.state import RunContext
    from knowledgeops_agent.harness.subagents.registry import SubagentRegistry

    path = tmp_path / "protocol.log"
    provider = configure(services, path)
    role = SubagentRegistry(services.sessions).create(**definition(enabled=True, tool_groups=["mcp"]))
    _, root, _ = enqueue(client)
    services.jobs.claim("parent", run_id=root["id"], generation=1)
    context = RunContext(run_id=root["id"], root_id=root["id"], worker_id="parent", jobs=services.jobs,
                         knowledge=services.knowledge, sources=services.sources, gateway=services.gateway)
    prepare_resources(context)
    child = services.jobs.start_child(root["id"], "从服务核对资料", operation_key="child-input", subagent=role)
    ChildRuns(services.jobs).wait(root["id"], "parent", [child["id"]])
    saver = InMemorySaver()
    first = execute_run(child["id"], 1, services=services, checkpointer=saver, worker_id="actual-child")
    assert first["status"] == "waiting_input"
    assert services.jobs.get_run(root["id"])["status"] == "waiting_children"
    question = client.get(f"/api/runs/{root['id']}/questions").json()[0]
    assert question["run_id"] == child["id"]
    if paused:
        services.jobs.pause(root["id"])
    assert client.post(f"/api/runs/{root['id']}/questions/{question['id']}/answer", json={
        "expected_revision": 1, "kind": "decline", "value": "",
    }).status_code == 200
    if paused:
        assert services.jobs.get_run(child["id"])["status"] == "paused"
        assert services.jobs.get_run(root["id"])["status"] == "paused"
        services.jobs.resume(root["id"], budgets={})
    current = services.jobs.get_run(child["id"])
    assert current["status"] == "queued"
    second = execute_run(child["id"], current["generation"], services=services, checkpointer=saver, worker_id="child-next")
    assert second["status"] == "completed" and provider.requests == 2
    assert path.read_text().splitlines() == ["first", "quantity:decline"]
    assert services.jobs.get_run(root["id"])["status"] == "queued"
