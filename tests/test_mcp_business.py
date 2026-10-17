"""验证配置许可与调用回执；本地MCP夹具没有模型或外部业务权限。"""

import importlib
import sys
from pathlib import Path

import pytest
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

runtime_services = _runtime_services


def registry(context):
    return importlib.import_module("knowledgeops_agent.harness.mcp.registry").MCPRegistry(
        context.jobs.sessions
    )


def configured(context, policy="read"):
    service = registry(context)
    row = service.create(
        "本地验证",
        {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(Path("tests/fixtures/mcp_probe.py").resolve())],
        },
    )
    row = service.discover(row["id"], expected_revision=1)
    assert row["tools"][0]["permission"] == "deny"
    return service.update(
        row["id"], expected_revision=row["revision"], enabled=True, permissions={"get_note": policy}
    )


def test_default_empty_discovery_is_not_permission_and_disable_fences_old_binding(runtime_services):
    _, context, _ = setup_run(runtime_services)
    service = registry(context)
    assert service.list() == []
    row = configured(context)
    frozen = service.snapshot()[0]
    assert service.check(frozen, "get_note")["permission"] == "read"
    disabled = service.update(row["id"], expected_revision=row["revision"], enabled=False)
    service.update(row["id"], expected_revision=disabled["revision"], enabled=True)
    with pytest.raises(PermissionError, match="撤回|许可"):
        service.check(frozen, "get_note")
    assert service.snapshot()[0]["authorization_epoch"] > frozen["authorization_epoch"]


def test_successful_call_has_source_and_same_call_id_replays_without_external_call(
    runtime_services, monkeypatch
):
    _, context, _ = setup_run(runtime_services)
    configured(context)
    from knowledgeops_agent.harness.mcp.calls import MCPCalls
    from knowledgeops_agent.harness.runtime.resources import prepare_resources

    prepare_resources(context)
    calls = MCPCalls(context)
    binding = context.resources["mcp"][0]
    result = calls.invoke(binding, "get_note", {"topic": "检索证据"}, call_id="one")
    assert result["status"] == "succeeded" and result["source_version_id"]
    source = context.sources.get_version(result["source_version_id"])
    assert source["kind"] == "mcp"

    async def unexpected(*args, **kwargs):
        raise AssertionError("已成功的调用不能重做")

    monkeypatch.setattr(calls, "_execute", unexpected)
    assert calls.invoke(binding, "get_note", {"topic": "检索证据"}, call_id="one") == result
    with pytest.raises(ValueError, match="输入|复用"):
        calls.invoke(binding, "get_note", {"topic": "变化"}, call_id="one")


def test_side_effect_waits_for_bound_approval_and_unknown_result_is_never_retried(
    runtime_services, monkeypatch
):
    _, context, run = setup_run(runtime_services)
    configured(context, "confirm")
    from knowledgeops_agent.harness.mcp.calls import MCPApprovalRequired, MCPCalls
    from knowledgeops_agent.harness.runtime.resources import prepare_resources

    prepare_resources(context)
    calls = MCPCalls(context)
    binding = context.resources["mcp"][0]
    with pytest.raises(MCPApprovalRequired):
        calls.invoke(binding, "get_note", {"topic": "需要确认"}, call_id="approval")
    receipt = calls.list()[0]
    assert receipt["status"] == "pending"
    with pytest.raises(ValueError, match="绑定"):
        calls.decide(receipt["id"], expected_hash="wrong", approve=True)
    calls.decide(receipt["id"], expected_hash=receipt["input_hash"], approve=True)
    # 模拟发出请求后进程死亡，回执已标running但没有外部结果。
    from knowledgeops_agent.persistence.models import MCPCall

    with context.jobs.sessions.begin() as session:
        session.get(MCPCall, receipt["id"]).status = "running"

    async def unexpected(*args, **kwargs):
        raise AssertionError("未知结果不能重试")

    monkeypatch.setattr(calls, "_execute", unexpected)
    result = calls.invoke(binding, "get_note", {"topic": "需要确认"}, call_id="approval")
    assert result["status"] == "unknown"
    assert calls.list()[0]["status"] == "unknown"
    # 模型改一个调用ID不能绕过未知结果屏障，再次执行相同外部动作。
    with pytest.raises(PermissionError, match="未知"):
        calls.invoke(binding, "get_note", {"topic": "需要确认"}, call_id="new-id")


@pytest.mark.parametrize("cancel", [False, True], ids=["pending", "cancelled"])
def test_pending_approval_blocks_manual_resume_until_user_decides(runtime_services, cancel):
    _, context, run = setup_run(runtime_services)
    configured(context, "confirm")
    from knowledgeops_agent.harness.mcp.calls import MCPApprovalRequired, MCPCalls
    from knowledgeops_agent.harness.runtime.resources import prepare_resources

    prepare_resources(context)
    calls = MCPCalls(context)
    with pytest.raises(MCPApprovalRequired):
        calls.invoke(context.resources["mcp"][0], "get_note", {"topic": "等待"}, call_id="wait")
    context.jobs.finish(
        run["id"], context.worker_id, status="interrupted", outcome={"kind": "waiting_approval"}
    )
    if cancel:
        # 现行合同：取消已撤回未执行提议；显式恢复不能复活旧许可。
        context.jobs.cancel(run["id"])
        receipt = calls.list()[0]
        assert receipt["status"] == "denied"
        with pytest.raises(ValueError, match="决定"):
            calls.decide(receipt["id"], expected_hash=receipt["input_hash"], approve=True)
        assert context.jobs.resume(run["id"])["status"] == "queued"
        assert calls.list()[0]["status"] == "denied"
        return
    assert calls.list()[0]["status"] == "pending"
    with pytest.raises(ValueError, match="确认"):
        context.jobs.resume(run["id"])
    receipt = calls.list()[0]
    # 仍在等待的调用只能由明确决定继续；拒绝不授权执行外部动作。
    calls.decide(receipt["id"], expected_hash=receipt["input_hash"], approve=False)
    assert calls.list()[0]["status"] == "denied"


def test_knowledge_only_never_loads_mcp_tools(runtime_services):
    from dataclasses import replace

    from knowledgeops_agent.harness.runtime.resources import prepare_resources

    _, context, first = setup_run(runtime_services)
    configured(context)
    context.jobs.finish(first["id"], context.worker_id)
    run = context.jobs.enqueue(
        first["conversation_id"], "只用本知识库", request_key="no-mcp", knowledge_only=True
    )
    context.jobs.claim(context.worker_id, run_id=run["id"], generation=1)
    context = replace(context, root_id=run["id"], run_id=run["id"])
    prepare_resources(context)
    from knowledgeops_agent.harness.mcp.calls import mcp_tools

    assert mcp_tools(context) == []
