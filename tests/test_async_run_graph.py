"""真实主子图与 worker 调度的离线闭环；供应商用同步屏障证明同时进入三个请求。"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fixtures.execution_provider import ExecutionProvider
from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_api_worker import services as _services

from knowledgeops_agent.app.runs.children import ChildRuns
from knowledgeops_agent.app.runs.worker import execute_run
from knowledgeops_agent.harness.models.steps import _step_scope
from knowledgeops_agent.harness.subagents.async_tools import ASYNC_TOOLS
from knowledgeops_agent.harness.subagents.registry import SubagentRegistry
from knowledgeops_agent.harness.tools.policy import ToolPolicyService

services = _services


def configure(services, *, role_tools=None):
    SubagentRegistry(services.sessions).create(name="offline-reader", display_name="测试读者",
        description="独立核对一个问题", system_prompt="只报告本任务的核对结果。", tools=role_tools or [], skills=[], enabled=True)
    policy = ToolPolicyService(services.sessions)
    policy.update(expected_revision=policy.get()["revision"], enabled={f"builtin:{tool.name}": True for tool in ASYNC_TOOLS})
    conversation = services.jobs.create_conversation("离线并发")
    return services.jobs.enqueue(conversation["id"], "并行核对两个问题，然后整合结果", request_key="async-root")


def chunk(name, args, identifier, index=0):
    return {"name": name, "args": json.dumps(args), "id": identifier, "index": index}


class ConcurrentProvider(ExecutionProvider):
    def __init__(self, services, *, mode="async"):
        self.services, self.mode = services, mode
        self.barrier = Barrier(3) if mode == "async" else None
        self.arrived = set()

    def stream(self, messages, **kwargs):
        run_id = _step_scope.get()[0]
        row = self.services.jobs.get_run(run_id)
        if row["parent_id"]:
            self.arrived.add(run_id)
            if self.barrier:
                self.barrier.wait(timeout=30)
            yield AIMessageChunk("已核对本子任务的边界，结果仅适用于指定条件。")
            return
        tools = [m for m in messages if isinstance(m, ToolMessage)]
        if not tools:
            name = "task" if self.mode == "sync" else "start_async_task"
            yield AIMessageChunk("", tool_call_chunks=[chunk(name,
                {"description": f"独立核对问题 {index}", "subagent_type": "offline-reader"},
                f"launch-{index}", index) for index in range(2)])
            return
        if self.mode == "async" and not any(m.name == "wait_async_tasks" for m in tools):
            self.arrived.add(run_id)
            self.barrier.wait(timeout=30)
            ids = [json.loads(m.content)["task_id"] for m in tools if m.name == "start_async_task"]
            yield AIMessageChunk("", tool_call_chunks=[chunk("wait_async_tasks", {"task_ids": ids}, "wait-children")])
            return
        yield AIMessageChunk("已整合两份子任务结果，并保留各自适用条件。")


def test_main_and_two_children_reach_model_together_then_event_resumes(services):
    root = configure(services)
    provider = ConcurrentProvider(services)
    services.gateway.enabled, services.gateway.client = True, provider
    saver = InMemorySaver()
    with ThreadPoolExecutor(max_workers=3) as pool:
        main = pool.submit(execute_run, root["id"], 1, services=services, checkpointer=saver)
        deadline = time.monotonic() + 30
        while len(children := ChildRuns(services.jobs).list(root["id"])) < 2 and time.monotonic() < deadline:
            if main.done():
                pytest.fail(f"主任务提前退出：{main.result()}")
            time.sleep(.02)
        assert len(children) == 2
        child_futures = [pool.submit(execute_run, row["task_id"], 1, services=services, checkpointer=saver) for row in children]
        main.result(timeout=45)
        results = [future.result(timeout=45) for future in child_futures]
    assert all(result["status"] == "completed" for result in results), results
    assert provider.arrived == {root["id"], *(row["task_id"] for row in children)}
    queued = services.jobs.get_run(root["id"])
    assert queued["status"] == "queued"
    finished = execute_run(root["id"], queued["generation"], services=services, checkpointer=saver)
    assert finished["status"] == "completed", finished
    assert len(ChildRuns(services.jobs).list(root["id"])) == 2
    assert all(row["root_id"] == root["id"] for row in services.gateway.ledger.records(root_id=root["id"]))


def test_instruction_arriving_during_child_response_blocks_old_tool_proposal(services):
    root = configure(services, role_tools=["list_knowledge"])

    class UpdatedProvider(ConcurrentProvider):
        child_requests = []

        def stream(self, messages, **kwargs):
            row = self.services.jobs.get_run(_step_scope.get()[0])
            if not row["parent_id"]:
                yield from super().stream(messages, **kwargs)
                return
            self.child_requests.append(messages)
            if row["authority_epoch"] == 0:
                ChildRuns(self.services.jobs).update(root["id"], row["id"], "不要列出现有文档，只解释前置条件",
                    request_key="arrived-in-flight", origin="human")
                yield AIMessageChunk("", tool_call_chunks=[chunk("list_knowledge", {}, "stale-list")])
                return
            yield AIMessageChunk("已根据追加要求解释前置条件。")

    provider = UpdatedProvider(services, mode="implicit")
    services.gateway.enabled, services.gateway.client = True, provider
    saver = InMemorySaver()
    assert execute_run(root["id"], 1, services=services, checkpointer=saver)["status"] == "waiting_children"
    first = ChildRuns(services.jobs).list(root["id"])[0]
    result = execute_run(first["task_id"], 1, services=services, checkpointer=saver)
    assert result["status"] == "completed", result
    assert len(provider.child_requests) == 2
    assert any("不要列出现有文档" in str(m.content) for m in provider.child_requests[1])
    assert not any(e["kind"] == "tool_started" and e["data"].get("name") == "list_knowledge"
                   for e in services.jobs.events(first["task_id"]))
    instructions = ChildRuns(services.jobs).instructions(root["id"], first["task_id"])
    assert len(instructions) == 1 and instructions[0]["status"] == "applied"
    assert all(row["root_id"] == root["id"] for row in services.gateway.ledger.records(root_id=root["id"]))


def test_explicit_guidance_wakes_waiting_parent_without_finishing_or_rerunning_children(services):
    root = configure(services)

    class GuidedProvider(ConcurrentProvider):
        def stream(self, messages, **kwargs):
            current = self.services.jobs.get_run(root["id"])
            if current["applied_authority_epoch"] == 0:
                yield from super().stream(messages, **kwargs)
                return
            children = ChildRuns(self.services.jobs).list(root["id"])
            if any(child["status"] != "cancelled" for child in children):
                yield AIMessageChunk("", tool_call_chunks=[chunk("cancel_async_task", {"task_id": child["task_id"]},
                                                               f"cancel-{index}", index) for index, child in enumerate(children)])
            else:
                yield AIMessageChunk("已按你的新要求取消两个子任务。")

    services.gateway.enabled, services.gateway.client = True, GuidedProvider(services, mode="implicit")
    saver = InMemorySaver()
    waiting = execute_run(root["id"], 1, services=services, checkpointer=saver)
    assert waiting["status"] == "waiting_children"
    queued = services.jobs.enqueue(root["conversation_id"], "取消两个子任务，只说明现在进展", request_key="guide-wait")
    assert services.jobs.get_run(root["id"])["status"] == "waiting_children", "普通消息不得自动插入"
    services.jobs.guide(queued["id"], target_id=root["id"], target_generation=waiting["generation"])
    ready = services.jobs.get_run(root["id"])
    assert ready["status"] == "queued"
    result = execute_run(root["id"], ready["generation"], services=services, checkpointer=saver)
    assert result["status"] == "completed", result
    assert result["authority_epoch"] == result["applied_authority_epoch"] == 1
    assert all(child["status"] == "cancelled" for child in ChildRuns(services.jobs).list(root["id"]))
    assert all(row["run_id"] == root["id"] for row in services.gateway.ledger.records(root_id=root["id"]))


@pytest.mark.parametrize("mode", ["sync", "implicit"])
def test_sync_task_and_early_final_answer_wait_durably_without_thread_pool(services, mode):
    root = configure(services)
    provider = ConcurrentProvider(services, mode=mode)
    services.gateway.enabled, services.gateway.client = True, provider
    saver = InMemorySaver()
    first = execute_run(root["id"], 1, services=services, checkpointer=saver)
    assert first["status"] == "waiting_children", first
    children = ChildRuns(services.jobs).list(root["id"])
    assert len(children) == 2 and all(row["status"] == "queued" for row in children)
    for child in children:
        assert execute_run(child["task_id"], 1, services=services, checkpointer=saver)["status"] == "completed"
    current = services.jobs.get_run(root["id"])
    assert current["status"] == "queued"
    finished = execute_run(root["id"], current["generation"], services=services, checkpointer=saver)
    assert finished["status"] == "completed", finished
    assert len(ChildRuns(services.jobs).list(root["id"])) == 2
