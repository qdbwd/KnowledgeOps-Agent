"""在真实create_agent中验证工具可见性、执行撤回和检查点恢复；模型为离线替身。"""

import json

import pytest
from fixtures.subagent_dispatch import drive_graph
from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.harness.tools.policy import ToolPolicyService

runtime_services = _runtime_services


def test_disabled_builtin_and_middleware_tools_are_not_assembled(runtime_services):
    module, context, _ = setup_run(runtime_services)
    policy = ToolPolicyService(context.jobs.sessions).update(
        expected_revision=0,
        enabled={
            "builtin:read_source": False,
            "middleware:read_file": False,
            "middleware:task": False,
        },
    )
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    names = set(graph.nodes["tools"].bound.tools_by_name)
    assert not {"read_source", "read_file", "task"} & names
    assert "search_knowledge" in names
    assert context.resources["tool_policy"] == policy
    assert {row["name"] for row in context.tool_catalog} == names


def test_live_revoke_then_reenable_does_not_reoffer_to_old_run(runtime_services):
    module, context, run = setup_run(runtime_services)
    policy = ToolPolicyService(context.jobs.sessions)

    class Provider:
        def bind_tools(self, tools):
            assert "read_source" not in {tool.name for tool in tools}
            return self

        def stream(self, messages, **kwargs):
            yield AIMessageChunk("本轮不读取原始材料。")

    context.gateway.client = Provider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    policy.update(expected_revision=0, enabled={"builtin:read_source": False})
    policy.update(expected_revision=1, enabled={"builtin:read_source": True})
    graph.invoke(
        {"messages": module.initial_messages(context)},
        {"configurable": {"thread_id": run["checkpoint_id"]}},
        context=context,
    )
    assert context.resources["tool_policy"]["revision"] == 0


@pytest.mark.parametrize(
    "name,identifier",
    [
        ("read_source", "builtin:read_source"),
        ("read_file", "middleware:read_file"),
        ("task", "middleware:task"),
    ],
)
def test_withdraw_after_model_choice_blocks_original_handler(runtime_services, name, identifier):
    module, context, run = setup_run(runtime_services)
    service = ToolPolicyService(context.jobs.sessions)
    segment = context.sources.get_source(run["input"]["source_ids"][0])["segments"][0]["id"]
    args = {
        "read_source": {"segment_id": segment},
        "read_file": {"file_path": "/skills/evidence-check/SKILL.md"},
        "task": {"subagent_type": "researcher", "description": "读取材料"},
    }[name]

    class Provider:
        def bind_tools(self, tools):
            self.names = {tool.name for tool in tools}
            return self

        def stream(self, messages, **kwargs):
            observations = [m for m in messages if isinstance(m, ToolMessage)]
            if observations:
                assert observations[-1].status == "error"
                assert observations[-1].tool_call_id == "withdrawn-tool"
                assert name not in self.names
                yield AIMessageChunk("该工具已停用，操作未执行。")
                return
            assert name in self.names
            # 模拟管理请求在模型选择工具后、工具执行前撤回许可。
            service.update(expected_revision=0, enabled={identifier: False})
            yield AIMessageChunk(
                "",
                tool_call_chunks=[
                    {
                        "name": name,
                        "args": json.dumps(args),
                        "id": "withdrawn-tool",
                        "index": 0,
                    }
                ],
            )

    context.gateway.client = Provider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    graph.invoke(
        {"messages": module.initial_messages(context)},
        {"configurable": {"thread_id": run["checkpoint_id"]}},
        context=context,
    )
    assert context.sources.evidence_catalog(context.root_id) == []
    assert not [r for r in context.jobs.list_runs(root_id=run["id"]) if r["parent_id"]]
    assert not [e for e in context.jobs.events(run["id"]) if e["kind"] == "skill_read"]


def test_pending_checkpoint_tool_is_denied_after_rebuild(runtime_services):
    module, context, run = setup_run(runtime_services)
    segment = context.sources.get_source(run["input"]["source_ids"][0])["segments"][0]["id"]

    class Provider:
        def bind_tools(self, tools):
            return self

        def stream(self, messages, **kwargs):
            if any(isinstance(m, ToolMessage) for m in messages):
                yield AIMessageChunk("原定读取未执行，可以重新选择后续操作。")
            else:
                yield AIMessageChunk(
                    "",
                    tool_call_chunks=[
                        {
                            "name": "read_source",
                            "args": json.dumps({"segment_id": segment}),
                            "id": "pending-read",
                            "index": 0,
                        }
                    ],
                )

    context.gateway.client = Provider()
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": run["checkpoint_id"]}}
    graph = module.build_agent(context, checkpointer=saver)
    graph.invoke(
        {"messages": module.initial_messages(context)},
        config,
        context=context,
        interrupt_after=["model"],
    )
    assert graph.get_state(config).next
    service = ToolPolicyService(context.jobs.sessions)
    service.update(expected_revision=0, enabled={"builtin:read_source": False})
    service.update(expected_revision=1, enabled={"builtin:read_source": True})
    graph = module.build_agent(context, checkpointer=saver)
    result = graph.invoke(None, config, context=context)
    receipt = next(m for m in result["messages"] if isinstance(m, ToolMessage))
    assert receipt.tool_call_id == "pending-read" and receipt.status == "error"
    assert context.sources.evidence_catalog(context.root_id) == []


def test_main_selection_and_child_groups_share_global_ceiling(runtime_services):
    """主Agent可以只委派；子角色依分组读取，但仍不能获得全局关闭的工具。"""
    from knowledgeops_agent.harness.subagents.registry import SubagentRegistry

    module, context, run = setup_run(runtime_services)
    SubagentRegistry(context.jobs.sessions).create(
        name="group-reader",
        display_name="分组阅读者",
        description="按分组读取材料",
        system_prompt="GROUP-READER",
        tools=[],
        tool_groups=["materials_read"],
        skills=[],
        enabled=True,
    )
    ToolPolicyService(context.jobs.sessions).update(
        expected_revision=0,
        enabled={"builtin:inspect_material": False},
        main_tools=["middleware:task"],
        main_tool_groups=[],
    )
    segment = context.sources.get_source(run["input"]["source_ids"][0])["segments"][0]["id"]

    class Provider:
        def __init__(self, names=None):
            self.names = names

        def bind_tools(self, tools):
            return Provider({tool.name for tool in tools})

        def stream(self, messages, **kwargs):
            is_child = "GROUP-READER" in str(messages[0].content)
            observations = [m for m in messages if isinstance(m, ToolMessage)]
            # 本用例保留零版本全局许可，新目录成员不能自动扩权。
            assert self.names == ({"list_materials", "read_source"} if is_child else {"task"})
            if observations:
                if is_child:
                    evidence = json.loads(observations[-1].content)
                    yield AIMessageChunk("重复消息需要幂等。" + evidence["citation"])
                else:
                    assert json.loads(observations[-1].content)["status"] == "completed"
                    yield AIMessageChunk("已收到只读子任务结果。")
                return
            yield AIMessageChunk(
                "",
                tool_call_chunks=[
                    {
                        "name": "read_source" if is_child else "task",
                        "args": json.dumps(
                            {"segment_id": segment}
                            if is_child
                            else {
                                "description": f"读取{segment}",
                                "subagent_type": "group-reader",
                            }
                        ),
                        "id": "child-read" if is_child else "group-delegation",
                        "index": 0,
                    }
                ],
            )

    context.gateway.client = Provider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    drive_graph(context, graph, {"messages": module.initial_messages(context)})
    children = [row for row in context.jobs.list_runs(root_id=run["id"]) if row["parent_id"]]
    assert len(children) == 1 and children[0]["status"] == "completed"


def test_revoke_during_context_processing_is_checked_before_provider(runtime_services, monkeypatch):
    """前置筛选后仍可能进行摘要；发送请求前的最后检查不能漏掉此期间的撤回。"""
    from knowledgeops_agent.harness.context.management import ContextMiddleware

    module, context, run = setup_run(runtime_services)
    original = ContextMiddleware.wrap_model_call

    def withdraw(self, request, handler):
        ToolPolicyService(context.jobs.sessions).update(
            expected_revision=0,
            enabled={"builtin:read_source": False},
        )
        return original(self, request, handler)

    monkeypatch.setattr(ContextMiddleware, "wrap_model_call", withdraw)

    class Provider:
        def bind_tools(self, tools):
            assert "read_source" not in {tool.name for tool in tools}
            return self

        def stream(self, messages, **kwargs):
            yield AIMessageChunk("这一轮先回答已知内容。")

    context.gateway.client = Provider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    graph.invoke(
        {"messages": module.initial_messages(context)},
        {"configurable": {"thread_id": run["id"]}},
        context=context,
    )


def test_mcp_revoke_uses_existing_registry_without_second_permission_store(runtime_services):
    from test_mcp_business import configured

    from knowledgeops_agent.harness.mcp.registry import MCPRegistry

    module, context, run = setup_run(runtime_services)
    binding = configured(context)

    class Provider:
        def bind_tools(self, tools):
            assert not any(tool.name.startswith("mcp_") for tool in tools)
            return self

        def stream(self, messages, **kwargs):
            yield AIMessageChunk("外部服务已停用，本轮不访问它。")

    context.gateway.client = Provider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    assert any(row["source"] == "mcp" for row in context.tool_catalog)
    MCPRegistry(context.jobs.sessions).update(
        binding["id"], expected_revision=binding["revision"], enabled=False
    )
    graph.invoke(
        {"messages": module.initial_messages(context)},
        {"configurable": {"thread_id": run["id"]}},
        context=context,
    )
    assert ToolPolicyService(context.jobs.sessions).get()["revision"] == 0


def test_legacy_resource_snapshot_uses_baseline_not_latest_grants(runtime_services):
    from knowledgeops_agent.harness.runtime.resources import content_hash
    from knowledgeops_agent.persistence.models import RunResourceSnapshot

    module, context, _ = setup_run(runtime_services)
    module.build_agent(context, checkpointer=InMemorySaver())
    # 构造0015前合法清单：它确实没有这个字段，不是假造当前策略修订。
    with context.jobs.sessions.begin() as session:
        row = session.get(RunResourceSnapshot, context.root_id)
        old = {key: value for key, value in row.content.items() if key != "tool_policy"}
        row.content, row.content_hash = old, content_hash(old)
    service = ToolPolicyService(context.jobs.sessions)
    service.update(expected_revision=0, enabled={"builtin:read_source": False})
    service.update(expected_revision=1, enabled={"builtin:read_source": True})
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    assert "read_source" not in graph.nodes["tools"].bound.tools_by_name
    assert "search_knowledge" in graph.nodes["tools"].bound.tools_by_name
    assert "tool_policy" not in context.resources
