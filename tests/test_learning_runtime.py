"""真实create_agent请求装配；替身只返回文字，检验运行实际看见的配置和来源。"""

import json
from dataclasses import replace

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_runtime import DraftProvider, setup_run
from test_runtime import runtime_services as _runtime_services

from knowledgeops_agent.harness.runtime.resources import prepare_resources
from knowledgeops_agent.knowledge.intent import ensure_publication_intent
from knowledgeops_agent.learning.profiles import LearningProfileService
from knowledgeops_agent.learning.tasks import LearningTaskService
from knowledgeops_agent.memory.service import MemoryService
from knowledgeops_agent.persistence.files import FileStore

runtime_services = _runtime_services


def test_learning_config_enters_real_graph_once_and_is_frozen(runtime_services, tmp_path):
    module, old_context, old_run = setup_run(runtime_services)
    jobs = old_context.jobs
    jobs.sessions.configure(info={"file_store": FileStore(tmp_path / "workspace")})
    jobs.cancel(old_run["id"])
    jobs.finish(old_run["id"], "worker-test", status="cancelled")
    task = LearningTaskService(jobs.sessions).create(
        conversation_id=old_run["conversation_id"],
        request_key="learning",
        title="数据库",
        goal="理解数据库事务",
        topic="数据库",
    )
    profiles = LearningProfileService(jobs.sessions)
    profile = profiles.get(task_id=task["id"])
    profiles.update(profile["id"], expected_revision=1, content="本任务重点：并发事务的执行顺序。")
    memory = MemoryService(jobs.sessions).create(
        "独特习惯：学习数据库时先给例子", scope="global", topic=""
    )
    run = jobs.enqueue(old_run["conversation_id"], "先解释事务", request_key="learning-message")
    assert jobs.claim("worker-test", run_id=run["id"], generation=1) is not None

    class Capture(DraftProvider):
        def __init__(self):
            self.requests = []

        def stream(self, messages, **kwargs):
            self.requests.append(messages)
            yield AIMessageChunk(content="事务把相关操作放在一起执行。")

    context = replace(old_context, run_id=run["id"], root_id=run["id"], resources=None)
    capture = Capture()
    context.gateway.client = capture
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    graph.invoke(
        {"messages": [HumanMessage(run["task"])]},
        {"configurable": {"thread_id": run["checkpoint_id"]}},
        context=context,
    )
    messages = capture.requests[-1]
    assert any(
        isinstance(m, SystemMessage) and "交互学习（interactive）" in m.text for m in messages
    )
    background = next(m for m in messages if m.additional_kwargs.get("knowledgeops_context_data"))
    value = json.loads(background.text.split("\n", 1)[1])
    assert value["learning"]["task"]["id"] == task["id"]
    assert "并发事务" in value["learning"]["configuration"]["profiles"][1]["content"]
    assert memory["id"] not in {m["id"] for m in value.get("preferences", [])}
    assert background.text.count("独特习惯") == 1
    profiles.update(profile["id"], expected_revision=2, content="后来的修改")
    frozen = profiles.effective(context.resources["learning"]["profiles"])
    assert "并发事务" in frozen["profiles"][1]["content"]


def test_chat_create_and_configure_use_one_intent_call_then_actual_model_background(
    runtime_services, tmp_path
):
    module, context, run = setup_run(runtime_services)
    context.jobs.sessions.configure(info={"file_store": FileStore(tmp_path / "workspace")})

    class Capture(DraftProvider):
        def __init__(self):
            self.intent_calls = 0
            self.requests = []

        def invoke(self, messages, **kwargs):
            self.intent_calls += 1
            payload = json.loads(messages[-1].text)
            return AIMessage(
                json.dumps(
                    {
                        "action": "none",
                        "clear": False,
                        "message_id": payload["message"]["id"],
                        "root_id": context.root_id,
                        "quote": "",
                        "changeset_id": None,
                        "expected_revision": None,
                        "confirm_impact": False,
                        "ui_conflict": False,
                        "continue_task": True,
                        "preferences": [],
                        "learning": [
                            {
                                "action": "create",
                                "clear": True,
                                "quote": payload["message"]["content"],
                                "fields": {"title": "材料整理", "goal": "先理解材料，再整理知识"},
                            },
                            {
                                "action": "configure",
                                "clear": True,
                                "quote": payload["message"]["content"],
                                "target_id": "created",
                                "fields": {"detail": "先解释已有概念的联系"},
                            },
                        ],
                    },
                    ensure_ascii=False,
                )
            )

        def stream(self, messages, **kwargs):
            self.requests.append(messages)
            yield AIMessageChunk(content="可以先明确材料中的概念，再讨论它们之间的联系。")

    capture = Capture()
    context.gateway.client = capture
    original = prepare_resources(context)
    assert "learning" not in original
    decision = ensure_publication_intent(context)
    assert decision["learning"]["errors"] == []
    assert ensure_publication_intent(context) == decision
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    graph.invoke(
        {"messages": [HumanMessage(run["task"])]},
        {"configurable": {"thread_id": run["checkpoint_id"]}},
        context=context,
    )
    assert capture.intent_calls == 1
    background = next(
        m for m in capture.requests[-1] if m.additional_kwargs.get("knowledgeops_context_data")
    )
    data = json.loads(background.text.split("\n", 1)[1])
    assert (
        data["learning"]["configuration"]["task_configuration"]["detail"]["value"]
        == "先解释已有概念的联系"
    )
    assert "learning" not in prepare_resources(context)
    assert any(
        isinstance(m, SystemMessage) and "交互学习（interactive）" in m.text
        for m in capture.requests[-1]
    )
