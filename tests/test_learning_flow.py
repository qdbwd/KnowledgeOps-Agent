"""委托两单元走真实worker、主/子图、版本和发布；供应商响应确定且零付费。"""

from fixtures.learning_provider import LearningProvider
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from test_api_worker import services as _services

from knowledgeops_agent.db import LearningCollaboration, LearningUnit, Run, SubagentDefinition
from knowledgeops_agent.file_store import FileStore
from knowledgeops_agent.learning_profiles import LearningProfileService
from knowledgeops_agent.learning_tasks import LearningTaskService
from knowledgeops_agent.subagent_registry import SubagentRegistry
from knowledgeops_agent.tool_policy import ToolPolicyService
from knowledgeops_agent.worker import execute_run

services = _services


def test_two_units_revision_review_pause_profile_update_and_publish(services, tmp_path):
    services.sessions.configure(info={"file_store": FileStore(tmp_path / "workspace")})
    with services.sessions.begin() as session:
        for role in ("reader", "reviewer"):
            row = SubagentDefinition(
                id="learning-" + role,
                name="learning-" + role,
                display_name=role,
                description="学习协作",
                system_prompt="独立核对固定材料，遵守结构化输出合同。",
                tools=["read_learning_draft"],
                resolved_tools=["read_learning_draft"],
                skills=[],
                enabled=True,
            )
            session.add(row)
            SubagentRegistry._save(session, row)
    ToolPolicyService(services.sessions).update(
        expected_revision=0,
        enabled={
            "builtin:learning_feedback": True,
            "builtin:read_learning_draft": True,
            "builtin:propose_learning_framework": True,
        },
    )
    provider = LearningProvider()
    services.gateway.client, services.gateway.enabled = provider, True
    tasks = LearningTaskService(services.sessions)
    conversation = services.jobs.create_conversation()
    task = tasks.create(
        conversation_id=conversation["id"],
        request_key="demo",
        title="离线协作演示",
        goal="证明工程流程",
        mode="delegated",
        units=[
            {"key": "one", "title": "第一单元", "goal": "完成首次读者与审读"},
            {"key": "two", "title": "第二单元", "goal": "带新配置继续", "dependencies": ["one"]},
        ],
    )
    task = tasks.confirm(task["id"], expected_revision=1, auto_publish=True)
    delivered = []

    def inject_real_feedback(unit, observations):
        # 在第一单元发布后、根运行结束前模拟真人点击“引导当前任务”。
        # 消息仍经真实enqueue/guide/解释/提取，不直接改配置或伪造用户证据。
        if delivered or not observations or observations[-1].name != "publish_changes":
            return
        current = next(
            r for r in services.jobs.list_runs() if r["status"] == "running" and not r["parent_id"]
        )
        message = services.jobs.enqueue(
            conversation["id"],
            "本任务请用SQL例子；以后解释先给具体例子。",
            request_key="real-guidance",
        )
        delivered.append(
            services.jobs.guide(message["id"], target_id=current["id"], target_generation=1)
        )

    provider.before_main_request = inject_real_feedback
    saver = InMemorySaver()
    first = next(r for r in services.jobs.list_runs() if r["status"] == "queued")
    result = execute_run(first["id"], 1, services=services, checkpointer=saver, worker_id="demo")
    assert result["status"] == "completed", result
    assert result["outcome"]["published_ids"]
    task = tasks.get(task["id"])
    assert task["configuration"]["examples"]["value"] == "使用SQL例子"
    assert provider.intent_calls == 1 and len(delivered) == 1
    from knowledgeops_agent.preferences import MemoryService

    assert len(MemoryService(services.sessions).list()) == 1
    paused = tasks.control(task["id"], expected_revision=task["revision"], action="pause")
    queued = next(r for r in services.jobs.list_runs() if r["status"] == "interrupted")
    assert (
        execute_run(queued["id"], 1, services=services, checkpointer=saver, worker_id="demo")
        is None
    )
    profiles = LearningProfileService(services.sessions)
    profile = profiles.get(task_id=task["id"])
    profiles.update(
        profile["id"],
        expected_revision=profile["revision"],
        content="第二单元请先给例子，这是人工反馈后的修改。",
    )
    tasks.control(task["id"], expected_revision=paused["revision"], action="resume")
    result = execute_run(queued["id"], 2, services=services, checkpointer=saver, worker_id="demo")
    assert result["status"] == "completed", result
    assert tasks.get(task["id"])["status"] == "completed"
    assert any(
        "第二单元请先给例子" in p["content"] for p in provider.configurations[-1]["profiles"]
    )
    with services.sessions() as session:
        units = list(
            session.scalars(select(LearningUnit).where(LearningUnit.task_id == task["id"]))
        )
        reports = list(session.scalars(select(LearningCollaboration)))
        assert all(
            u.status == "published" and u.model_calls > 0 and u.result_version_id for u in units
        )
        assert len(reports) == 4 and all(r.read_ranges == [[0, r.material_length]] for r in reports)
        assert len(list(session.scalars(select(Run).where(Run.origin == "automatic")))) == 2
    assert len([m for m in services.jobs.messages(conversation["id"]) if m["role"] == "user"]) == 1
