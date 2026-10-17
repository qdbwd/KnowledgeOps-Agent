"""子角色接入既有 MCP：冻结许可、真实独立领取与会话范围必须同时成立。"""

from dataclasses import replace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from test_mcp_session_business import content, register
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run
from test_subagent_api import definition

from knowledgeops_agent.app.runs.children import ChildRuns
from knowledgeops_agent.app.runs.service import RunPaused, SteeringPending
from knowledgeops_agent.harness.mcp.calls import MCPCalls, mcp_tools
from knowledgeops_agent.harness.mcp.registry import MCPRegistry
from knowledgeops_agent.harness.runtime.resources import prepare_resources
from knowledgeops_agent.harness.subagents.registry import SubagentRegistry
from knowledgeops_agent.harness.tools.catalog import mcp_tool_name

runtime_services = _runtime_services


def configured_child(services, *, groups=None):
    module, parent, root = setup_run(services)
    binding = register(parent)
    binding = MCPRegistry(parent.jobs.sessions).update(
        binding["id"], expected_revision=binding["revision"], permissions={"exit_process": "confirm"}
    )
    registry = SubagentRegistry(parent.jobs.sessions)
    role = registry.create(**definition(enabled=True, tool_groups=groups))
    prepare_resources(parent)
    child = parent.jobs.start_child(root["id"], "核对材料", operation_key="child-mcp", subagent=role)
    parent.jobs.claim("independent-child", run_id=child["id"], generation=1)
    context = replace(parent, run_id=child["id"], worker_id="independent-child")
    return module, parent, context, binding, role


def test_role_inherits_only_frozen_read_mcp_and_uses_own_worker_and_session(runtime_services):
    module, parent, child, binding, role = configured_child(runtime_services)
    graph = module.build_agent(child, checkpointer=InMemorySaver())
    names = set(graph.nodes["tools"].bound.tools_by_name)
    assert mcp_tool_name(binding["id"], "counter") in names
    assert mcp_tool_name(binding["id"], "exit_process") not in names
    second = parent.jobs.start_child(parent.root_id, "独立核对", operation_key="second", subagent=role)
    parent.jobs.claim("second-child", run_id=second["id"], generation=1)
    sibling = replace(parent, run_id=second["id"], worker_id="second-child")
    parent_result = MCPCalls(parent).invoke(binding, "counter", {}, call_id="parent-one")
    ChildRuns(parent.jobs).wait(parent.root_id, parent.worker_id, [child.run_id, sibling.run_id])
    first = MCPCalls(child).invoke(binding, "counter", {}, call_id="child-one")
    other = MCPCalls(sibling).invoke(binding, "counter", {}, call_id="sibling-one")
    again = MCPCalls(child).invoke(binding, "counter", {}, call_id="child-two")
    assert [content(row)["count"] for row in [parent_result, first, other, again]] == [1, 1, 1, 2]
    assert len({row["session"]["id"] for row in [parent_result, first, other]}) == 3
    assert again["session"]["id"] == first["session"]["id"]
    assert parent.jobs.get_run(parent.root_id)["tool_calls"] == 4
    assert MCPCalls(child).invoke(binding, "counter", {}, call_id="child-two") == again
    assert parent.jobs.get_run(parent.root_id)["tool_calls"] == 4
    with pytest.raises(PermissionError, match="只读"):
        MCPCalls(child).invoke(binding, "exit_process", {}, call_id="cannot-write")
    parent.jobs.pause(parent.root_id)
    with pytest.raises(RunPaused):
        MCPCalls(child).invoke(binding, "counter", {}, call_id="paused")


def test_explicit_role_groups_can_remove_mcp_and_direct_calls_cannot_bypass(runtime_services):
    _, _, child, binding, _ = configured_child(runtime_services, groups=[])
    assert mcp_tools(child) == []
    with pytest.raises(PermissionError, match="角色|MCP"):
        MCPCalls(child).invoke(binding, "counter", {}, call_id="unselected")
    assert MCPCalls(child).list() == []


def test_removing_inherited_mcp_revokes_prior_role_binding(runtime_services):
    _, _, child, binding, role = configured_child(runtime_services)
    registry = SubagentRegistry(child.jobs.sessions)
    narrowed = registry.update(role["id"], expected_revision=role["revision"], tool_groups=[])
    assert narrowed["authorization_epoch"] == role["authorization_epoch"] + 1
    with pytest.raises(PermissionError, match="撤回"):
        MCPCalls(child).invoke(binding, "counter", {}, call_id="revoked")
    assert MCPCalls(child).list() == []


def test_old_snapshot_does_not_gain_child_mcp_and_new_group_is_explicit(runtime_services):
    _, parent, child, binding, _ = configured_child(runtime_services, groups=["mcp"])
    assert mcp_tools(child)
    old = dict(parent.resources)
    old.pop("child_mcp", None)
    historical = replace(child, resources=old)
    assert mcp_tools(historical) == []
    with pytest.raises(PermissionError, match="快照|MCP"):
        MCPCalls(historical).invoke(binding, "counter", {}, call_id="old")


def test_new_child_instruction_fences_old_mcp_before_receipt_or_network(runtime_services):
    _, parent, child, binding, _ = configured_child(runtime_services)
    from knowledgeops_agent.persistence.models import Run

    with parent.jobs.sessions.begin() as session:
        row = session.get(Run, child.run_id)
        row.authority_epoch += 1
    with pytest.raises(SteeringPending):
        MCPCalls(child).invoke(binding, "counter", {}, call_id="stale", decision_epoch=0)
    assert MCPCalls(child).list() == []
