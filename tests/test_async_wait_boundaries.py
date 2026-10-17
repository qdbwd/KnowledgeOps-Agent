"""子任务等待真人时仍未收束，不能被终态名称、取消或主图最终回答绕过。"""

import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessageChunk
from langgraph.checkpoint.memory import InMemorySaver
from test_api_worker import services as _services
from test_async_run_graph import ConcurrentProvider, configure
from test_async_run_scheduling import family
from test_jobs import jobs as _jobs

from knowledgeops_agent.app.runs.children import ChildRuns
from knowledgeops_agent.app.runs.worker import execute_run
from knowledgeops_agent.harness.mcp.registry import MCPRegistry
from knowledgeops_agent.harness.models.steps import _step_scope
from knowledgeops_agent.persistence.models import MCPCall

jobs = _jobs
services = _services


def hold_children(service, children, kind):
    for index, child in enumerate(children):
        owner = f"waiting-child-{index}"
        assert service.claim(owner, run_id=child["id"], generation=child["generation"])
        service.finish(child["id"], owner, status="interrupted", outcome={"kind": kind})


@pytest.mark.parametrize("kind", ["waiting_approval", "execution_unknown", "waiting_input"])
def test_human_waiting_children_still_block_root_completion_and_third_child(jobs, kind):
    _, _, service, _, _ = jobs
    root, first, second = family(service)
    hold_children(service, [first, second], kind)
    with pytest.raises(ValueError, match="子任务"):
        service.finish(root["id"], "main", outcome={"answer": "尚未收束，不能交付"})
    with pytest.raises(ValueError, match="two"):
        service.start_child(root["id"], "不能绕过两个未收束子任务", operation_key="third")


@pytest.mark.parametrize("kind", ["waiting_approval", "execution_unknown", "waiting_input"])
def test_cancel_root_cancels_human_waiting_children_and_does_not_wake_it(jobs, kind):
    _, _, service, _, _ = jobs
    root, first, second = family(service)
    hold_children(service, [first, second], kind)
    ChildRuns(service).wait(root["id"], "main", [first["id"], second["id"]])
    result = service.cancel(root["id"])
    assert result["status"] == "cancelled"
    children = ChildRuns(service).list(root["id"])
    assert all(child["status"] == "cancelled" and child["cancel_requested"] for child in children)
    assert service.get_run(root["id"])["generation"] == 1


def test_graph_final_answer_waits_for_children_that_have_pending_human_work(services):
    root = configure(services)

    class HumanWaitingProvider(ConcurrentProvider):
        held = False

        def stream(self, messages, **kwargs):
            row = services.jobs.get_run(_step_scope.get()[0])
            children = ChildRuns(services.jobs).list(root["id"])
            if row["parent_id"] is None and len(children) == 2:
                if not self.held:
                    self.held = True
                    hold_children(services.jobs, [services.jobs.get_run(c["task_id"]) for c in children],
                                  "waiting_approval")
                yield AIMessageChunk("在子任务等待真人处理时错误地宣称完成。")
                return
            yield from super().stream(messages, **kwargs)

    services.gateway.enabled, services.gateway.client = True, HumanWaitingProvider(services, mode="implicit")
    result = execute_run(root["id"], 1, services=services, checkpointer=InMemorySaver())
    assert result["status"] == "waiting_children", result
    assert services.jobs.messages(root["conversation_id"])[-1]["role"] == "user"


def test_resume_root_checks_pending_mcp_calls_owned_by_children(jobs):
    _, _, service, sessions, _ = jobs
    root, first, second = family(service)
    connection = MCPRegistry(sessions).create("只用于待办记录，不启动服务", {
        "transport": "stdio", "command": sys.executable,
        "args": [str(Path("tests/fixtures/mcp_probe.py").resolve())],
    })
    hold_children(service, [first, second], "waiting_approval")
    with sessions.begin() as session:
        session.add(MCPCall(run_id=first["id"], connection_id=connection["id"], connection_revision=1,
                            tool_call_id="pending-child", tool_name="inspect", input_hash="a" * 64,
                            request={}, status="pending", authority_epoch=0))
    ChildRuns(service).wait(root["id"], "main", [first["id"], second["id"]])
    service.pause(root["id"])
    with pytest.raises(ValueError, match="MCP"):
        service.resume(root["id"])
    assert service.get_run(root["id"])["status"] == "paused"
    assert service.get_run(first["id"])["status"] == "interrupted"
