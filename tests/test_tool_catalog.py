"""统一目录的消费边界：真实构图、角色选项、MCP身份和分组权限。

这些断言防止只管理普通tools而漏掉中间件、重名覆盖及工具组扩大子角色许可。
图使用真实业务服务/隔离数据库；构图不调用供应商，也不连接任何外部MCP。
"""

from dataclasses import replace

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run
from test_subagent_runtime import role

from knowledgeops_agent.harness.subagents.registry import options
from knowledgeops_agent.persistence.models import Run

runtime_services = _runtime_services


def test_role_options_describe_shared_groups_without_adding_write_tools():
    """管理入口必须知道选项来自哪个组；共享目录不能给只读角色增加发布能力。"""
    result = options()
    by_name = {row["name"]: row for row in result["tools"]}
    assert by_name["read_source"].get("id") == "builtin:read_source"
    assert by_name["read_source"].get("group") == "materials_read"
    assert by_name["call_model"].get("source") == "builtin"
    assert set(by_name) == {
        "search_knowledge",
        "list_knowledge",
        "knowledge_history",
        "read_knowledge",
        "list_materials",
        "list_evidence",
        "inspect_material",
        "read_source",
        "read_tool_result",
        "update_plan",
        "search_web",
        "read_web",
        "call_model",
        "history_search",
        "history_read",
        "task_note",
        "memory_search",
        "memory_read",
        "read_learning_draft",
    }
    groups = {row["id"]: row["tool_ids"] for row in result.get("tool_groups", [])}
    assert groups.get("materials_read") == [
        "builtin:list_materials",
        "builtin:list_evidence",
        "builtin:inspect_material",
        "builtin:read_source",
    ]
    assert "knowledge_maintenance" not in groups
    assert "delegation" not in groups


@pytest.mark.parametrize("knowledge_only,auxiliary", [(False, False), (True, True)])
def test_effective_catalog_covers_actual_graph_including_middleware(
    runtime_services, knowledge_only, auxiliary
):
    """最终构图必须携带全部真实工具的目录，Schema直接来自原工具，不重写参数。"""
    module, context, _ = setup_run(runtime_services, auxiliary=auxiliary)
    role(context)
    with context.jobs.sessions.begin() as session:
        row = session.get(Run, context.root_id)
        row.input_data = {**row.input_data, "knowledge_only": knowledge_only}
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    actual = graph.nodes["tools"].bound.tools_by_name
    described = {row["name"]: row for row in getattr(context, "tool_catalog", [])}
    expected = {
        "search_knowledge",
        "list_knowledge",
        "knowledge_history",
        "read_knowledge",
        "list_materials",
        "inspect_material",
        "read_source",
        "read_tool_result",
        "update_plan",
        "prepare_changes",
        "publish_changes",
        "capture_conversation",
        "list_changesets",
        "read_changeset",
        "revise_changeset",
        "reject_changeset",
        "read_file",
        "task",
    }
    if not knowledge_only:
        expected |= {"search_web", "read_web"}
    if auxiliary:
        expected.add("call_model")
    assert set(described) == set(actual) == expected
    assert described["read_file"]["id"] == "middleware:read_file"
    assert described["task"]["source"] == "middleware"
    for name, row in described.items():
        assert row["schema"] == convert_to_openai_tool(actual[name])["function"]["parameters"]
    assert set(described["task"]["schema"]["properties"]) == {"description", "subagent_type"}
    assert len(context.gateway.ledger.records()) == 0


def test_child_directory_keeps_explicit_role_and_root_restrictions(runtime_services):
    """角色选了网页/辅助工具，也不能越过根任务关闭项；Skill读取仍被正确登记。"""
    module, context, _ = setup_run(runtime_services)
    with context.jobs.sessions.begin() as session:
        row = session.get(Run, context.root_id)
        row.input_data = {**row.input_data, "knowledge_only": True}
    configured = role(
        context, tools=["read_source", "read_web", "call_model"], skills=["evidence-check"]
    )
    module.build_agent(context, checkpointer=InMemorySaver())
    child = context.jobs.start_child(
        context.root_id, "只读材料", operation_key="catalog-child", subagent=configured
    )
    child_context = replace(context, run_id=child["id"])
    graph = module.build_agent(child_context, checkpointer=InMemorySaver())
    described = {row["name"]: row for row in getattr(child_context, "tool_catalog", [])}
    assert (
        set(described)
        == set(graph.nodes["tools"].bound.tools_by_name)
        == {
            "read_source",
            "read_file",
        }
    )
    assert "task" not in described
    assert len(context.gateway.ledger.records()) == 0
