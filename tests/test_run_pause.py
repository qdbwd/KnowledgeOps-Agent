"""暂停真实流/图/worker，不用延时模拟供应商，也不产生付费请求。"""

import json

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_api_worker import client as _client
from test_api_worker import enqueue
from test_api_worker import services as _services
from test_runtime import DraftProvider
from test_runtime import runtime_services as _runtime_services
from test_workspace_runtime import enable

from knowledgeops_agent.app.runs.worker import execute_run

client = _client
services = _services
runtime_services = _runtime_services


def test_queued_pause_resume_cancel_is_persistent_and_idempotent(client, services):
    _, run, _ = enqueue(client)
    url = f"/api/runs/{run['id']}"
    paused = client.post(url + "/pause")
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused" and paused.json()["pause_requested"]
    assert client.post(url + "/pause").json()["last_event"] == paused.json()["last_event"]
    assert services.jobs.claim("must-not-start", run_id=run["id"], generation=run["generation"]) is None
    resumed = client.post(url + "/resume", json={"budgets": {}})
    assert resumed.status_code == 202 and resumed.json()["status"] == "queued"
    assert not resumed.json()["pause_requested"]
    client.post(url + "/pause")
    assert client.post(url + "/cancel").json()["status"] == "cancelled"


class PauseProvider:
    invoke = DraftProvider.invoke

    def __init__(self, jobs, run_id):
        self.jobs, self.run_id = jobs, run_id
        self.requests = 0
        self.finished_stream = False

    def bind_tools(self, tools):
        return self

    def stream(self, messages, **kwargs):
        self.requests += 1
        if any(isinstance(message, ToolMessage) for message in messages):
            yield AIMessageChunk(content="已恢复并完成文件写入。")
            return
        yield AIMessageChunk(content="准备写入，")
        self.jobs.pause(self.run_id)
        yield AIMessageChunk(content="响应已完整接收。", tool_call_chunks=[{
            "name": "write_file", "id": "pause-write", "index": 0,
            "args": json.dumps({"file_path": "/work/result.txt", "content": "once"}),
        }])
        self.finished_stream = True


def test_worker_saves_entire_response_before_pause_and_resumes_original_tools(client, services, tmp_path):
    _, run, _ = enqueue(client)
    services.settings.workspace_root = tmp_path / "workspace"
    services.settings.workspace_root.mkdir()
    enable(services.sessions)
    provider = PauseProvider(services.jobs, run["id"])
    services.gateway.enabled, services.gateway.client = True, provider
    saver = InMemorySaver()
    paused = execute_run(run["id"], run["generation"], services=services, checkpointer=saver, worker_id="pause-worker")
    path = services.settings.workspace_root / "work/result.txt"
    assert paused["status"] == "paused"
    assert provider.finished_stream and provider.requests == 1 and not path.exists()
    config = {"configurable": {"thread_id": run["checkpoint_id"]}}
    values = saver.get(config)["channel_values"]
    answer = next(m for m in reversed(values["messages"]) if isinstance(m, AIMessage))
    assert answer.content == "准备写入，响应已完整接收。" and answer.tool_calls[0]["id"] == "pause-write"
    original_users = [m.content for m in values["messages"] if isinstance(m, HumanMessage)]
    resumed = client.post(f"/api/runs/{run['id']}/resume", json={"budgets": {}}).json()
    finished = execute_run(run["id"], resumed["generation"], services=services, checkpointer=saver, worker_id="pause-worker")
    assert finished["status"] == "completed" and path.read_text() == "once"
    assert provider.requests == 2 and finished["tool_calls"] == 1
    values = saver.get(config)["channel_values"]
    assert [m.content for m in values["messages"] if isinstance(m, HumanMessage)] == original_users


def test_approving_a_paused_run_does_not_resume_it(runtime_services, tmp_path):
    from test_execution_harness import graph_setup, resume_graph, save_wait

    module, context, run, graph, config = graph_setup(runtime_services, tmp_path, ["touch work/approved"])
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context)
    approvals, _ = save_wait(context, run, graph, config)
    assert context.jobs.pause(run["id"])["status"] == "paused"
    action = approvals.list()[0]
    approvals.decide(action["id"], expected_fingerprint=action["fingerprint"], approve=True)
    assert context.jobs.get_run(run["id"])["status"] == "paused"
    assert not (context.workspace_root / "work/approved").exists()
    context.jobs.resume(run["id"])
    resume_graph(context, graph, config)
    assert (context.workspace_root / "work/approved").exists()


def test_paused_guidance_is_saved_and_old_tool_proposal_never_runs(client, services, tmp_path):
    _, run, _ = enqueue(client)
    services.settings.workspace_root = tmp_path / "workspace"
    services.settings.workspace_root.mkdir()
    enable(services.sessions)
    provider = PauseProvider(services.jobs, run["id"])
    services.gateway.enabled, services.gateway.client = True, provider
    saver = InMemorySaver()
    paused = execute_run(run["id"], run["generation"], services=services, checkpointer=saver, worker_id="pause-worker")
    queued = services.jobs.enqueue(run["conversation_id"], "不要写文件了，只解释原方案", request_key="paused-guidance")
    assert services.jobs.claim("queued-worker", run_id=queued["id"], generation=1) is None
    delivery = services.jobs.guide(queued["id"], target_id=run["id"], target_generation=paused["generation"])
    assert delivery["status"] == "pending" and services.jobs.get_run(run["id"])["status"] == "paused"
    resumed = client.post(f"/api/runs/{run['id']}/resume", json={"budgets": {}}).json()
    finished = execute_run(run["id"], resumed["generation"], services=services, checkpointer=saver, worker_id="pause-worker")
    assert finished["status"] == "completed"
    assert not (services.settings.workspace_root / "work/result.txt").exists()
    values = saver.get({"configurable": {"thread_id": run["checkpoint_id"]}})["channel_values"]
    assert any(isinstance(m, HumanMessage) and m.content == "不要写文件了，只解释原方案" for m in values["messages"])
    assert any(isinstance(m, ToolMessage) and m.tool_call_id == "pause-write" and m.status == "error" for m in values["messages"])
