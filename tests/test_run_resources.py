"""运行资源冻结必须发生在首次模型请求之前，恢复不能悄悄换用新规则。"""

import importlib
from dataclasses import replace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

runtime_services = _runtime_services


def resource_service(context):
    module = importlib.import_module("knowledgeops_agent.harness.runtime.resources")
    return module.ResourceService(context.jobs.sessions)


def test_snapshot_is_immutable_and_child_inherits_root_resources(runtime_services, monkeypatch):
    _, context, _ = setup_run(runtime_services)
    service = resource_service(context)
    first = service.ensure(context)
    assert {"guided-learning", "evidence-check", "knowledge-maintenance"} <= {
        item["name"] for item in first["skills"]
    }
    assert first["policy"]["content"]
    # 部署新提示后，恢复/委派仍须使用原任务冻结的行为指令，不能暗换成新文案。
    monkeypatch.setattr("knowledgeops_agent.harness.runtime.agent.SYSTEM_PROMPT", "部署后的新行为指令")
    assert first["prompts"]["system"] != "部署后的新行为指令"
    monkeypatch.setattr(
        "knowledgeops_agent.harness.runtime.resources.load_knowledge_policy",
        lambda: pytest.fail("a frozen run must not reload changed content"),
    )
    child = context.jobs.start_child(context.root_id, "核对来源", operation_key="child-resource")
    assert service.ensure(replace(context, run_id=child["id"])) == first
    assert service.ensure(context) == first


def test_old_run_cannot_fabricate_resource_snapshot(runtime_services):
    from knowledgeops_agent.persistence.models import Run

    _, context, _ = setup_run(runtime_services)
    with context.jobs.sessions.begin() as session:
        session.get(Run, context.run_id).format_version = 1
    with pytest.raises(RuntimeError, match="旧格式"):
        resource_service(context).ensure(context)
    assert len(context.gateway.ledger.records()) == 0


def test_missing_required_policy_fails_before_intent_model_request(runtime_services, monkeypatch):
    from types import SimpleNamespace

    from knowledgeops_agent.app.runs.worker import execute_run

    jobs, knowledge, sources, ledger = runtime_services
    _, prepared, _ = setup_run(runtime_services, mode="ask")
    jobs.finish(prepared.run_id, "worker-test", status="completed")
    run = jobs.enqueue(
        jobs.create_conversation()["id"], "请解释知识库", request_key="missing-policy"
    )
    policy = importlib.import_module("knowledgeops_agent.knowledge.policy")
    monkeypatch.setattr(policy, "_POLICY_PATH", policy._POLICY_PATH.with_name("missing.md"))
    services = SimpleNamespace(
        jobs=jobs,
        knowledge=knowledge,
        sources=sources,
        gateway=prepared.gateway,
        settings=SimpleNamespace(workspace_root=None, workspace_execute_timeout=60,
                                 workspace_output_limit_bytes=10485760),
    )
    result = execute_run(
        run["id"], 1, services=services, checkpointer=InMemorySaver(), worker_id="w"
    )
    assert result["status"] == "failed"
    assert len(ledger.records()) == 0


def test_resource_snapshot_has_no_live_clients_or_secrets_and_detects_corruption(runtime_services):
    from knowledgeops_agent.harness.tools.policy import baseline_policy
    from knowledgeops_agent.persistence.models import RunResourceSnapshot

    _, context, _ = setup_run(runtime_services)
    service = resource_service(context)
    manifest = service.ensure(context)
    assert manifest["format_version"] == 2
    assert set(manifest) == {
        "format_version",
        "policy",
        "skills",
        "prompts",
        "model",
        "preferences",
        "memory",
        "mcp",
        "child_mcp",
        "subagents",
        "tool_policy",
        "web_search",
    }
    assert manifest["tool_policy"] == baseline_policy()
    with context.jobs.sessions.begin() as session:
        row = session.scalar(select(RunResourceSnapshot))
        row.content = {**row.content, "skills": []}
    with pytest.raises(RuntimeError, match="哈希"):
        service.ensure(context)


def test_actual_agent_reads_skill_and_records_version_receipt(runtime_services):
    import json

    from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage

    module, context, _ = setup_run(runtime_services)

    class Reader:
        def bind_tools(self, tools):
            assert "read_file" in {tool.name for tool in tools}
            return self

        def stream(self, messages, **kwargs):
            assert "guided-learning" in str(messages[0].content)
            if not any(isinstance(message, ToolMessage) for message in messages):
                yield AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "read_file",
                            "args": json.dumps(
                                {"file_path": "/skills/guided-learning/SKILL.md", "limit": 100}
                            ),
                            "id": "actual-skill",
                            "index": 0,
                        }
                    ],
                )
            else:
                assert "交互学习与主题梳理" in str(messages[-1].content)
                yield AIMessageChunk(content="可以先理解主要问题，再讨论你关心的部分。")

    context.gateway.client = Reader()
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    state = graph.invoke(
        {"messages": [HumanMessage("先学习框架")]},
        {"configurable": {"thread_id": context.run_id}},
        context=context,
    )
    assert state["messages"][-1].text
    reads = [
        row["data"] for row in context.jobs.events(context.run_id) if row["kind"] == "skill_read"
    ]
    assert reads and reads[0]["path"] == "/skills/guided-learning/SKILL.md"
    assert len(reads[0]["sha256"]) == 64


def test_revoked_core_stops_and_revoked_skill_cannot_be_read(runtime_services):
    from knowledgeops_agent.persistence.models import ResourceRevocation

    _, context, _ = setup_run(runtime_services)
    service = resource_service(context)
    manifest = service.ensure(context)
    skill = manifest["skills"][0]
    policy = manifest["policy"]
    with context.jobs.sessions.begin() as session:
        for kind, item in [("policy", policy), ("skill", skill)]:
            session.add(
                ResourceRevocation(
                    kind=kind,
                    resource=item["resource"],
                    content_hash=item["sha256"],
                    reason="测试撤回",
                )
            )
    assert not service.skill_available(manifest, skill["resource"])
    with pytest.raises(RuntimeError, match="撤回"):
        service.validate(context, manifest)
