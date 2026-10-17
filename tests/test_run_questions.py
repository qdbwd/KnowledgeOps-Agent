"""真实模型循环/worker 的补问闭环；替身只代替收费供应商。"""

import json
import subprocess
import sys

import pytest
from fixtures.reliability_provider import QuestionsProvider
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_api_worker import client as _client
from test_api_worker import enqueue
from test_api_worker import services as _services
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run
from test_workspace_runtime import enable

from knowledgeops_agent.app.runs.worker import execute_run
from knowledgeops_agent.harness.tools.policy import ToolPolicyService

client = _client
services = _services
runtime_services = _runtime_services


def waiting(client, services, tmp_path):
    _, run, _ = enqueue(client)
    services.settings.workspace_root = tmp_path / "workspace"
    services.settings.workspace_root.mkdir()
    enable(services.sessions)
    policy = ToolPolicyService(services.sessions)
    policy.update(expected_revision=policy.get()["revision"], enabled={"builtin:ask_clarification": True})
    provider = QuestionsProvider()
    services.gateway.enabled, services.gateway.client = True, provider
    saver = InMemorySaver()
    stopped = execute_run(run["id"], 1, services=services, checkpointer=saver, worker_id="question-worker")
    assert stopped["status"] == "waiting_input"
    assert len(provider.requests) == 1
    assert not (services.settings.workspace_root / "work/premature.txt").exists()
    assert stopped["tool_calls"] == 1
    rows = client.get(f"/api/runs/{run['id']}/questions").json()
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    return stopped, rows[0], provider, saver


@pytest.mark.parametrize("kind,value", [("text", "需要适当例子"), ("option", "初学者"), ("decline", "")])
def test_question_answer_resumes_original_run_once(client, services, tmp_path, kind, value):
    run, question, provider, saver = waiting(client, services, tmp_path)
    path = f"/api/runs/{run['id']}/questions/{question['id']}/answer"
    body = {"expected_revision": question["revision"], "kind": kind, "value": value}
    answer = client.post(path, json=body)
    assert answer.status_code == 200
    queued = services.jobs.get_run(run["id"])
    assert queued["status"] == "queued"
    assert client.post(path, json=body).json() == answer.json()
    assert services.jobs.get_run(run["id"])["generation"] == queued["generation"]
    result = execute_run(run["id"], queued["generation"], services=services, checkpointer=saver, worker_id="question-worker")
    assert result["status"] == "completed" and len(provider.requests) == 2
    assert not (services.settings.workspace_root / "work/premature.txt").exists()
    content = next(m.content for m in provider.requests[-1] if isinstance(m, ToolMessage) and m.name == "ask_clarification")
    assert json.loads(content)["answer"] == {"kind": kind, "value": value}


def test_question_rejects_stale_invalid_answers_and_cancel(client, services, tmp_path):
    run, question, _, _ = waiting(client, services, tmp_path)
    path = f"/api/runs/{run['id']}/questions/{question['id']}/answer"
    assert client.post(path, json={"expected_revision": 99, "kind": "text", "value": "a"}).status_code == 409
    assert client.post(path, json={"expected_revision": 1, "kind": "option", "value": "不存在的选项"}).status_code == 409
    client.post(f"/api/runs/{run['id']}/cancel")
    assert client.post(path, json={"expected_revision": 1, "kind": "text", "value": "a"}).status_code == 409
    assert services.jobs.get_run(run["id"])["status"] == "cancelled"


def test_answer_while_paused_stays_paused_until_explicit_resume(client, services, tmp_path):
    run, question, _, saver = waiting(client, services, tmp_path)
    client.post(f"/api/runs/{run['id']}/pause")
    answer = client.post(f"/api/runs/{run['id']}/questions/{question['id']}/answer", json={
        "expected_revision": question["revision"], "kind": "text", "value": "按初学者组织",
    })
    assert answer.status_code == 200 and services.jobs.get_run(run["id"])["status"] == "paused"
    resumed = client.post(f"/api/runs/{run['id']}/resume", json={"budgets": {}}).json()
    assert execute_run(run["id"], resumed["generation"], services=services, checkpointer=saver, worker_id="question-worker")["status"] == "completed"


def test_new_guidance_invalidates_question_and_resumes_without_answer(client, services, tmp_path):
    run, question, _, saver = waiting(client, services, tmp_path)
    queued = services.jobs.enqueue(run["conversation_id"], "不整理正文了，解释这两个选择即可", request_key="replace-question")
    services.jobs.guide(queued["id"], target_id=run["id"], target_generation=run["generation"])
    current = services.jobs.get_run(run["id"])
    assert current["status"] == "queued"
    assert client.get(f"/api/runs/{run['id']}/questions").json()[0]["status"] == "stale"
    finished = execute_run(run["id"], current["generation"], services=services, checkpointer=saver, worker_id="question-worker")
    assert finished["status"] == "completed"
    assert not (services.settings.workspace_root / "work/premature.txt").exists()


def test_question_continues_in_new_process_using_only_pg_and_files(client, services, tmp_path):
    _, run, _ = enqueue(client)
    enable(services.sessions)
    policy = ToolPolicyService(services.sessions)
    policy.update(expected_revision=policy.get()["revision"], enabled={"builtin:ask_clarification": True})
    with services.engine.connect() as connection:
        schema = connection.exec_driver_sql("SELECT current_schema()").scalar_one()

    def launch(generation):
        result = subprocess.run([
            sys.executable, "tests/reliability_process.py", schema, services.gateway.ledger.scope,
            str(tmp_path), run["id"], str(generation),
        ], capture_output=True, text=True, timeout=150)
        assert result.returncode == 0, result.stderr[-4000:]
        return json.loads(result.stdout)

    first = launch(1)
    assert first["run"]["status"] == "waiting_input" and first["model_requests"] == 1
    # 上一进程已经退出，没有图对象、供应商实例或活连接可沿用。
    question = client.get(f"/api/runs/{run['id']}/questions").json()[0]
    answer = client.post(f"/api/runs/{run['id']}/questions/{question['id']}/answer", json={
        "expected_revision": question["revision"], "kind": "text", "value": "先讲基础概念",
    })
    assert answer.status_code == 200
    resumed = services.jobs.get_run(run["id"])
    second = launch(resumed["generation"])
    assert second["run"]["status"] == "completed" and second["model_requests"] == 1
    assert second["run"]["tool_calls"] == 1
    assert not (tmp_path / "workspace/work/premature.txt").exists()


def test_old_tools_checkpoint_also_blocks_question_siblings(runtime_services, tmp_path, monkeypatch):
    from langgraph.types import Command

    from knowledgeops_agent.app.runs.questions import RunQuestions
    from knowledgeops_agent.harness.runtime.clarification import ClarificationMiddleware

    module, context, run = setup_run(runtime_services)
    context.workspace_root = tmp_path / "workspace"
    enable(context.jobs.sessions)
    policy = ToolPolicyService(context.jobs.sessions)
    policy.update(expected_revision=policy.get()["revision"], enabled={"builtin:ask_clarification": True})
    context.gateway.client = QuestionsProvider()
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": run["checkpoint_id"]}}
    with monkeypatch.context() as patch:
        patch.setattr(ClarificationMiddleware, "after_model", lambda self, state, runtime: None)
        old_graph = module.build_agent(context, checkpointer=saver)
        old_graph.invoke({"messages": module.initial_messages(context)}, config, context=context, interrupt_before=["tools"])
    graph = module.build_agent(context, checkpointer=saver)
    graph.invoke(None, config, context=context)
    assert not (context.workspace_root / "work/premature.txt").exists()
    questions = RunQuestions(context.jobs)
    snapshot = graph.get_state(config)
    assert questions.record_interrupts(context, snapshot)
    questions.settle_wait(context, {})
    question = questions.list(run["id"])[0]
    from knowledgeops_agent.app.runs.questions import QuestionAnswer

    questions.answer(run["id"], question["id"], QuestionAnswer(expected_revision=1, kind="text", value="初学者"))
    current = context.jobs.get_run(run["id"])
    context.jobs.claim(context.worker_id, run_id=run["id"], generation=current["generation"])
    graph.invoke(Command(resume=questions.resume_answers(context, snapshot)), config, context=context)
    assert not (context.workspace_root / "work/premature.txt").exists()
