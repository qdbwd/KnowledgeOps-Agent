"""直接使用上游 task 与真实业务服务，验证目录、隔离、冻结及恢复；供应商离线替身。"""

import json
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

runtime_services = _runtime_services


def role(context, name="reader", **values):
    from knowledgeops_agent.subagent_registry import SubagentRegistry

    return SubagentRegistry(context.jobs.sessions).create(
        name=name,
        display_name=name,
        description=f"核对{name}任务的材料",
        system_prompt=f"ROLE:{name}。只报告核对结果与证据。",
        tools=values.pop("tools", ["read_source"]),
        skills=values.pop("skills", []),
        enabled=True,
        **values,
    )


def call(name, args, identifier):
    return {
        "name": name,
        "args": json.dumps(args, ensure_ascii=False),
        "id": identifier,
        "index": 0,
    }


def test_new_root_reuses_upstream_task_tool(runtime_services):
    module, context, _ = setup_run(runtime_services)
    context.gateway.client = object()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    names = set(graph.nodes["tools"].bound.tools_by_name)
    assert "task" in names
    upstream_task = graph.nodes["tools"].bound.tools_by_name["task"]
    assert upstream_task.func.__module__ == "deepagents.middleware.subagents"
    assert set(upstream_task.args) == {"description", "subagent_type"}


def test_configured_role_uses_frozen_tools_skills_and_replays_child(runtime_services):
    from knowledgeops_agent.delegation import delegate_one

    module, context, run = setup_run(runtime_services, task="主对话隐私标识")
    from knowledgeops_agent.db import Run

    with context.jobs.sessions.begin() as session:
        row = session.get(Run, context.root_id)
        row.input_data = {**row.input_data, "knowledge_only": True}
    configured = role(
        context,
        tools=["read_source", "read_web", "call_model"],
        skills=["evidence-check"],
    )
    root = context.jobs.get_run(context.root_id)
    segment = context.sources.get_source(root["input"]["source_ids"][0])["segments"][0]["id"]

    class Provider:
        def __init__(self, names=frozenset()):
            self.names = names

        def bind_tools(self, tools):
            return type(self)(frozenset(tool.name for tool in tools))

        def stream(self, messages, **kwargs):
            system = str(messages[0].content)
            names = self.names
            is_child = "ROLE:reader" in system
            observations = [m for m in messages if isinstance(m, ToolMessage)]
            if not is_child:
                assert "ROLE:reader" not in system
                if not observations:
                    yield AIMessageChunk(
                        content="",
                        tool_call_chunks=[
                            call(
                                "task",
                                {"subagent_type": "reader", "description": f"读取{segment}并报告"},
                                "configured-task",
                            )
                        ],
                    )
                else:
                    result = json.loads(observations[-1].content)
                    assert result["status"] == "completed"
                    assert result["agent_name"] == "reader"
                    yield AIMessageChunk(content="已收到子任务报告。")
                return
            assert "主对话隐私标识" not in str(messages)
            assert names == {"read_source", "read_file"}
            assert "evidence-check" in system
            assert "/skills/guided-learning/" not in system
            if not observations:
                yield AIMessageChunk(
                    content="",
                    tool_call_chunks=[call("read_source", {"segment_id": segment}, "child-read")],
                )
            else:
                evidence = json.loads(observations[-1].content)
                yield AIMessageChunk(content="重复消息需要幂等。" + evidence["citation"])

    context.gateway.client = Provider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    result = graph.invoke(
        {"messages": [HumanMessage(run["task"])]},
        {"configurable": {"thread_id": run["id"]}},
        context=context,
    )
    assert result["messages"][-1].content == "已收到子任务报告。"
    children = [r for r in context.jobs.list_runs(root_id=run["id"]) if r["parent_id"]]
    assert len(children) == 1
    assert children[0]["input"]["subagent"] == configured
    before = len(context.gateway.ledger.records())
    replay = delegate_one(context, configured, f"读取{segment}并报告", "configured-task")
    assert replay["run_id"] == children[0]["id"]
    assert len(context.gateway.ledger.records()) == before


def test_role_edits_do_not_rewrite_frozen_definition_and_disable_revokes(runtime_services):
    from knowledgeops_agent.delegation import delegate_one
    from knowledgeops_agent.subagent_registry import SubagentRegistry

    module, context, _ = setup_run(runtime_services)
    original = role(context)
    registry = SubagentRegistry(context.jobs.sessions)
    module.build_agent(context, checkpointer=InMemorySaver())
    edited = registry.update(original["id"], expected_revision=1, system_prompt="新版指令")
    frozen = next(row for row in context.resources["subagents"] if row["name"] == "reader")
    assert frozen["system_prompt"] == original["system_prompt"]
    assert registry.validate_binding(original) == original
    registry.update(original["id"], expected_revision=edited["revision"], enabled=False)
    with pytest.raises(PermissionError):
        delegate_one(context, original, "核对资料", "disabled-task")
    assert not [r for r in context.jobs.list_runs(root_id=context.root_id) if r["parent_id"]]
    assert len(context.gateway.ledger.records()) == 0


def test_parallel_upstream_task_call_ids_remain_distinct(runtime_services):
    module, context, run = setup_run(runtime_services)
    role(context, "alpha", tools=[])
    role(context, "beta", tools=[])

    class Provider:
        def __init__(self, names=frozenset()):
            self.names = names

        def bind_tools(self, tools):
            return type(self)(frozenset(tool.name for tool in tools))

        def stream(self, messages, **kwargs):
            system = str(messages[0].content)
            if "ROLE:alpha" in system or "ROLE:beta" in system:
                assert [m.content for m in messages if isinstance(m, HumanMessage)] == [
                    "核对甲" if "ROLE:alpha" in system else "核对乙"
                ]
                assert not self.names
                yield AIMessageChunk(content="甲结果" if "ROLE:alpha" in system else "乙结果")
            elif any(isinstance(m, ToolMessage) for m in messages):
                outputs = {
                    m.tool_call_id: json.loads(m.content)
                    for m in messages
                    if isinstance(m, ToolMessage)
                }
                assert outputs["task-alpha"]["agent_name"] == "alpha"
                assert outputs["task-beta"]["agent_name"] == "beta"
                yield AIMessageChunk(content="两个子任务返回。")
            else:
                yield AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        call(
                            "task",
                            {"subagent_type": "alpha", "description": "核对甲"},
                            "task-alpha",
                        ),
                        {
                            **call(
                                "task",
                                {"subagent_type": "beta", "description": "核对乙"},
                                "task-beta",
                            ),
                            "index": 1,
                        },
                    ],
                )

    context.gateway.client = Provider()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    graph.invoke(
        {"messages": [HumanMessage(run["task"])]},
        {"configurable": {"thread_id": run["id"]}},
        context=context,
    )
    children = [r for r in context.jobs.list_runs(root_id=run["id"]) if r["parent_id"]]
    assert len(children) == 2
    assert {r["input"]["subagent"]["name"] for r in children} == {"alpha", "beta"}
    assert len({r["checkpoint_id"] for r in children}) == 2
    assert len(context.gateway.ledger.records()) == 4


def test_child_revocation_stops_before_next_tool_or_model(runtime_services):
    from knowledgeops_agent.delegation import delegate_one
    from knowledgeops_agent.subagent_registry import SubagentRegistry

    module, context, _ = setup_run(runtime_services)
    configured = role(context)
    registry = SubagentRegistry(context.jobs.sessions)

    class Provider:
        def __init__(self, names=frozenset()):
            self.names = names

        def bind_tools(self, tools):
            return type(self)(frozenset(tool.name for tool in tools))

        def stream(self, messages, **kwargs):
            registry.update(configured["id"], expected_revision=1, enabled=False)
            yield AIMessageChunk(
                content="",
                tool_call_chunks=[
                    call("read_source", {"segment_id": "never-read"}, "revoke-after-model")
                ],
            )

    context.gateway.client = Provider()
    module.build_agent(context, checkpointer=InMemorySaver())
    result = delegate_one(context, configured, "核对资料", "revoked-during-run")
    assert result["status"] == "failed"
    assert len(context.gateway.ledger.records()) == 1
    events = context.jobs.events(result["run_id"])
    assert not [e for e in events if e["kind"] == "tool_started"]


def test_new_role_not_in_old_snapshot_and_child_cannot_delegate(runtime_services):
    from knowledgeops_agent.delegation import delegate_one

    module, context, _ = setup_run(runtime_services)
    module.build_agent(context, checkpointer=InMemorySaver())
    added_later = role(context)
    with pytest.raises(PermissionError, match="冻结"):
        delegate_one(context, added_later, "晚加入的角色", "too-late")
    child = context.jobs.start_child(context.run_id, "旧研究者", operation_key="legacy-child")
    with pytest.raises(PermissionError, match="recursive"):
        delegate_one(replace(context, run_id=child["id"]), added_later, "递归", "recursive")
    assert len(context.gateway.ledger.records()) == 0


def test_legacy_snapshot_keeps_legacy_tool_only(runtime_services):
    from knowledgeops_agent.db import RunResourceSnapshot
    from knowledgeops_agent.run_resources import content_hash

    module, context, _ = setup_run(runtime_services)
    module.build_agent(context, checkpointer=InMemorySaver())
    with context.jobs.sessions.begin() as session:
        snapshot = session.get(RunResourceSnapshot, context.root_id)
        value = dict(snapshot.content)
        value.pop("subagents")
        snapshot.content = value
        snapshot.content_hash = content_hash(value)
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    names = set(graph.nodes["tools"].bound.tools_by_name)
    assert "delegate_research" in names
    assert "task" not in names


def test_upstream_command_report_is_redacted_offloaded_and_keeps_state(runtime_services):
    from types import SimpleNamespace

    from langgraph.types import Command

    module, context, _ = setup_run(runtime_services)
    context.gateway.sensitive_values = ["TEST_SECRET_NOT_A_REAL_KEY"]
    content = "证据报告。" * 3000 + "TEST_SECRET_NOT_A_REAL_KEY"
    command = Command(
        update={
            "messages": [ToolMessage(content=content, tool_call_id="large-task")],
            "plan": [{"step": "核对", "status": "done"}],
        },
        goto="model",
    )
    request = SimpleNamespace(
        runtime=SimpleNamespace(context=context),
        state={},
        tool_call={
            "id": "large-task",
            "name": "task",
            "args": {"description": "核对资料", "subagent_type": "reader"},
        },
    )
    result = module.RuntimeControlMiddleware().wrap_tool_call(request, lambda _: command)
    assert result.goto == "model"
    assert result.update["plan"] == command.update["plan"]
    message = result.update["messages"][0]
    assert message.tool_call_id == "large-task"
    payload = json.loads(message.content)
    assert payload["delivery"] == "tool_output_artifact"
    artifact = context.jobs.read_artifact(
        payload["artifact_id"], root_id=context.root_id, start=12000
    )
    assert "TEST_SECRET_NOT_A_REAL_KEY" not in str(artifact)
    assert "[REDACTED]" in str(artifact)
