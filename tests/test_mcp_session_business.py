"""会话池与真实 Run/许可/回执协作；使用隔离 PG 和本地 stdio 服务。"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import select
from test_mcp_session_protocol import assert_process_exited, stateful_binding
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.app.runs.service import RunStopped
from knowledgeops_agent.harness.mcp.calls import MCPCalls
from knowledgeops_agent.harness.mcp.registry import MCPRegistry
from knowledgeops_agent.persistence.models import MCPCall

runtime_services = _runtime_services


def register(context):
    registry = MCPRegistry(context.jobs.sessions)
    row = registry.create("本地会话验证", stateful_binding()["config"])
    row = registry.discover(row["id"], expected_revision=1)
    return registry.update(
        row["id"],
        expected_revision=row["revision"],
        enabled=True,
        permissions={"counter": "read", "exit_process": "read"},
    )


def content(result):
    assert result["status"] == "succeeded", result
    return json.loads(result["preview"])["artifact"]["structured_content"]


def test_chat_persists_across_runs_while_other_chats_and_child_runs_are_isolated(runtime_services):
    _, context, root = setup_run(runtime_services)
    binding = register(context)
    calls = MCPCalls(context)
    first = calls.invoke(binding, "counter", {}, call_id="first")
    assert content(first)["count"] == 1
    # 同一工具调用恢复只读回执，不会再次增加服务端计数。
    assert calls.invoke(binding, "counter", {}, call_id="first") == first
    second = calls.invoke(binding, "counter", {}, call_id="second")
    assert content(second)["count"] == 2
    assert second["session"]["id"] == first["session"]["id"] and second["session"]["reused"]
    assert second["session"]["protocol_version"] == "2025-11-25"
    child = context.jobs.start_child(root["id"], "子任务", operation_key="child-session")
    context.jobs.claim("session-child", run_id=child["id"], generation=child["generation"])
    child_result = MCPCalls(replace(context, run_id=child["id"], worker_id="session-child")).invoke(
        binding, "counter", {}, call_id="child-first"
    )
    assert content(child_result)["count"] == 1
    assert child_result["session"]["id"] != first["session"]["id"]
    context.jobs.finish(child["id"], "session-child")
    context.jobs.finish(root["id"], context.worker_id)

    following = context.jobs.enqueue(
        root["conversation_id"], "接着使用服务", request_key="following"
    )
    context.jobs.claim(context.worker_id, run_id=following["id"], generation=1)
    next_context = replace(context, root_id=following["id"], run_id=following["id"])
    third = MCPCalls(next_context).invoke(binding, "counter", {}, call_id="following-first")
    assert content(third)["count"] == 3 and third["session"]["reused"]
    context.jobs.finish(following["id"], context.worker_id)
    _, other_context, _ = setup_run(runtime_services)
    other = MCPCalls(other_context).invoke(binding, "counter", {}, call_id="other-first")
    assert content(other)["count"] == 1
    assert other["session"]["id"] != first["session"]["id"]


def test_database_revocation_closes_idle_worker_connection_without_another_tool_call(
    runtime_services,
):
    _, context, _ = setup_run(runtime_services)
    binding = register(context)
    first = MCPCalls(context).invoke(binding, "counter", {}, call_id="first")
    pid = content(first)["pid"]
    registry = MCPRegistry(context.jobs.sessions)
    registry.update(binding["id"], expected_revision=binding["revision"], enabled=False)
    # 真实维护协程读取 PG 撤回，不手动调用 sweep，也不靠下一次 invoke 才释放。
    assert_process_exited(pid)
    with pytest.raises(PermissionError):
        MCPCalls(context).invoke(binding, "counter", {}, call_id="revoked")


@pytest.mark.parametrize("stop", ["cancel", "revoke"])
def test_in_flight_interruption_marks_result_unknown_and_closes_state(runtime_services, stop):
    _, context, _ = setup_run(runtime_services)
    binding = register(context)
    calls = MCPCalls(context)
    first = calls.invoke(binding, "counter", {}, call_id="first")
    pid = content(first)["pid"]
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(calls.invoke, binding, "counter", {"delay": 20}, call_id="slow")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with context.jobs.sessions() as session:
                row = session.scalar(select(MCPCall).where(MCPCall.tool_call_id == "slow"))
                if row is not None and row.status == "running":
                    break
            assert not result.done()
            time.sleep(0.05)
        else:
            raise AssertionError("MCP call did not start")
        if stop == "cancel":
            context.jobs.cancel(context.run_id)
            with pytest.raises(RunStopped):
                result.result(timeout=10)
        else:
            MCPRegistry(context.jobs.sessions).update(
                binding["id"], expected_revision=binding["revision"], enabled=False
            )
            assert result.result(timeout=10)["status"] == "unknown"
    assert_process_exited(pid)
    receipt = next(row for row in calls.list() if row["tool_call_id"] == "slow")
    assert receipt["status"] == "unknown"


def test_service_dies_after_call_was_sent_keeps_unknown_receipt_and_blocks_retry(runtime_services):
    _, context, _ = setup_run(runtime_services)
    binding = register(context)
    calls = MCPCalls(context)
    first = calls.invoke(binding, "counter", {}, call_id="first")
    result = calls.invoke(binding, "exit_process", {}, call_id="crash")
    assert result["status"] == "unknown"
    assert_process_exited(content(first)["pid"])
    with pytest.raises(PermissionError, match="未知"):
        calls.invoke(binding, "exit_process", {}, call_id="different-id")
