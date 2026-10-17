"""用户主动引导：默认排队、绑定运行代次、立即阻止新写入，安全点才采纳。"""

import json

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_jobs import jobs as _jobs
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

jobs = _jobs
runtime_services = _runtime_services


def pair(service):
    conversation = service.create_conversation()["id"]
    current = service.enqueue(
        conversation, "按材料直接保存", request_key="active", publication_mode="direct"
    )
    service.claim("w", run_id=current["id"], generation=1)
    queued = service.enqueue(conversation, "先别保存，补充原理解释", request_key="steer")
    return current, queued


def test_default_queue_does_not_interrupt_but_explicit_guidance_blocks_writes(jobs):
    module, db, service, sessions, _ = jobs
    current, queued = pair(service)
    with sessions.begin() as session:
        service.publication_guard(session, current["id"], "w")
        assert session.get(db.TaskGrant, current["id"])
    delivery = service.guide(queued["id"], target_id=current["id"], target_generation=1)
    assert delivery["status"] == "pending"
    assert service.get_run(queued["id"])["status"] == "cancelled"
    assert service.guide(queued["id"], target_id=current["id"], target_generation=1) == delivery
    with sessions.begin() as session:
        assert session.get(db.TaskGrant, current["id"]) is None
        with pytest.raises(module.RunStopped, match="引导"):
            service.publication_guard(session, current["id"], "w")


def test_stale_target_or_different_conversation_keeps_original_queue(jobs):
    _, _, service, _, _ = jobs
    current, queued = pair(service)
    with pytest.raises(ValueError, match="代次"):
        service.guide(queued["id"], target_id=current["id"], target_generation=2)
    other = service.enqueue(
        service.create_conversation()["id"], "其他会话", request_key="other-guide"
    )
    with pytest.raises(ValueError, match="同一会话"):
        service.guide(other["id"], target_id=current["id"], target_generation=1)
    assert service.get_run(queued["id"])["status"] == "queued"
    assert service.get_run(other["id"])["status"] == "queued"


def test_resume_does_not_apply_guidance_to_an_unselected_generation(jobs):
    _, _, service, _, _ = jobs
    current, queued = pair(service)
    delivery = service.guide(queued["id"], target_id=current["id"], target_generation=1)
    service.finish(current["id"], "w", status="interrupted")
    service.resume(current["id"])
    assert service.guidance(current["id"])[0]["id"] == delivery["id"]
    assert service.guidance(current["id"])[0]["status"] == "stale"


class SteeringProvider:
    """真实图和事务，仅模型为脚本：在第一轮请求进行中收到引导。"""

    def __init__(self, context, run, *, tool_call=True):
        self.context, self.run, self.tool_call = context, run, tool_call
        self.turn = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        payload = json.loads(messages[-1].content)
        return AIMessage(
            json.dumps(
                {
                    "action": "preview_task",
                    "clear": True,
                    "message_id": payload["message"]["id"],
                    "root_id": payload["root_id"],
                    "quote": payload["message"]["content"],
                    "changeset_id": None,
                    "expected_revision": None,
                    "confirm_impact": False,
                    "ui_conflict": False,
                    "continue_task": True,
                }
            )
        )

    def stream(self, messages, **kwargs):
        self.turn += 1
        if self.turn == 1:
            queued = self.context.jobs.enqueue(
                self.run["conversation_id"],
                "不要保存，改为解释运行机制",
                request_key="during-model",
            )
            self.context.jobs.guide(queued["id"], target_id=self.run["id"], target_generation=1)
            if self.tool_call:
                yield AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "prepare_changes",
                            "args": json.dumps({"operations": []}),
                            "id": "stale-write",
                            "index": 0,
                        }
                    ],
                )
            else:
                yield AIMessageChunk(content="旧方向的回答")
        else:
            assert any(
                isinstance(m, HumanMessage) and m.content == "不要保存，改为解释运行机制"
                for m in messages
            )
            if self.tool_call:
                result = next(
                    m
                    for m in messages
                    if isinstance(m, ToolMessage) and m.tool_call_id == "stale-write"
                )
                assert "未执行" in result.text
            yield AIMessageChunk(content="模型决定下一步，程序执行工具并把结果返回给模型。")


@pytest.mark.parametrize("tool_call", [False, True])
def test_guidance_during_model_request_is_applied_before_tools_or_final_answer(
    runtime_services, tool_call
):
    module, context, run = setup_run(runtime_services, mode="direct")
    context.gateway.client = SteeringProvider(context, run, tool_call=tool_call)
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    state = graph.invoke(
        {"messages": [HumanMessage(run["task"])]},
        {"configurable": {"thread_id": run["id"]}},
        context=context,
    )
    assert "模型决定下一步" in state["messages"][-1].text
    delivery = context.jobs.guidance(run["id"])[0]
    assert delivery["status"] == "applied"
    assert context.jobs.publication_intent(run["id"])["mode"] == "preview"
    assert context.knowledge.list_drafts(root_id=run["id"]) == []
    assert len([m for m in state["messages"] if m.id == "guidance-" + delivery["message_id"]]) == 1


def test_applied_guidance_reconstructs_state_after_database_checkpoint_gap(runtime_services):
    from types import SimpleNamespace

    from knowledgeops_agent.app.runs.guidance import GuidanceMiddleware
    from knowledgeops_agent.harness.runtime.resources import prepare_resources

    _, context, run = setup_run(runtime_services)
    prepare_resources(context)
    context.gateway.client = SteeringProvider(context, run)
    queued = context.jobs.enqueue(run["conversation_id"], "不要保存", request_key="checkpoint-gap")
    context.jobs.guide(queued["id"], target_id=run["id"], target_generation=1)
    middleware = GuidanceMiddleware()
    first = middleware.before_model({"messages": []}, SimpleNamespace(context=context))
    used = len(context.gateway.ledger.records())
    second = middleware.before_model({"messages": []}, SimpleNamespace(context=context))
    assert first["messages"] == second["messages"]
    assert len(context.gateway.ledger.records()) == used == 1


def test_draft_transaction_cannot_write_while_guidance_pending(runtime_services):
    from knowledgeops_agent.app.runs.service import SteeringPending

    _, context, run = setup_run(runtime_services)
    queued = context.jobs.enqueue(run["conversation_id"], "等一下", request_key="no-new-draft")
    context.jobs.guide(queued["id"], target_id=run["id"], target_generation=1)
    with pytest.raises(SteeringPending):
        context.knowledge.create_draft(
            run["id"],
            [{"action": "create", "title": "暂缓", "body": "尚未授权的草稿"}],
            guard=lambda session: context.jobs.publication_guard(session, run["id"], "worker-test"),
        )
