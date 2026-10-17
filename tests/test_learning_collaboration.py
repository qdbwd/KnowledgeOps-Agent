"""真实主/子图与PG，只有供应商输出为离线脚本；审读门检查具体版本和来源。"""

import json
from types import SimpleNamespace

import pytest
from fixtures.subagent_dispatch import drive_delegation, drive_graph
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.harness.subagents.registry import SubagentRegistry
from knowledgeops_agent.harness.tools.policy import ToolPolicyService
from knowledgeops_agent.learning.collaboration import (
    LearningCollaborationService,
    learning_feedback,
)
from knowledgeops_agent.learning.tasks import LearningTaskService
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.persistence.models import LearningUnit, Run, SubagentDefinition

runtime_services = _runtime_services


def tool_chunk(name, args, identifier):
    return AIMessageChunk(
        content="",
        tool_call_chunks=[
            {
                "name": name,
                "args": json.dumps(args, ensure_ascii=False),
                "id": identifier,
                "index": 0,
            }
        ],
    )


class CollaborationProvider:
    def __init__(self, draft_id, *, publish=False, skip_read=False):
        self.draft_id, self.publish, self.skip_read = draft_id, publish, skip_read
        self.child_inputs = []

    def bind_tools(self, tools):
        # 供应商绑定是每次请求的独立视图，子图不能覆盖父图的可见工具。
        return SimpleNamespace(
            stream=lambda messages, **kwargs: self.stream(
                messages, tools=[convert_to_openai_tool(t) for t in tools], **kwargs
            )
        )

    def stream(self, messages, **kwargs):
        names = {t["function"]["name"] for t in kwargs.get("tools", [])}
        observations = [m for m in messages if isinstance(m, ToolMessage)]
        if "learning_feedback" not in names:
            self.child_inputs.append(messages)
            assert "publish_changes" not in names and "task" not in names
            reader = any("你是本次独立reader" in m.text for m in messages)
            if not self.skip_read:
                pages = [
                    json.loads(m.content) for m in observations if m.name == "read_learning_draft"
                ]
                offset = pages[-1]["next_start"] if pages else 0
                if offset is not None:
                    yield tool_chunk("read_learning_draft", {"start": offset}, f"read-{offset}")
                    return
            report = (
                {"sufficient": True, "questions": [], "summary": "所需前置与例子已提供"}
                if reader
                else {
                    "verdict": "pass",
                    "findings": [],
                    "reader_resolutions": [],
                    "summary": "离线审读响应，工程测试用",
                }
            )
            yield AIMessageChunk(content=json.dumps(report, ensure_ascii=False))
        elif not observations:
            yield tool_chunk(
                "learning_feedback",
                {"role": "reader", "changeset_id": self.draft_id, "expected_revision": 1},
                "reader-call",
            )
        elif len(observations) == 1:
            result = json.loads(observations[-1].content)
            assert result["status"] == "completed", result
            yield tool_chunk(
                "learning_feedback",
                {"role": "reviewer", "changeset_id": self.draft_id, "expected_revision": 1},
                "review-call",
            )
        elif len(observations) == 2 and self.publish:
            result = json.loads(observations[-1].content)
            assert result["status"] == "completed", result
            yield tool_chunk(
                "publish_changes",
                {"changeset_id": self.draft_id, "expected_revision": 1},
                "publish-call",
            )
        else:
            yield AIMessageChunk(content="本单元已完成审读。")


@pytest.fixture
def collaboration(runtime_services, tmp_path):
    module, context, run = setup_run(runtime_services)
    sessions = context.jobs.sessions
    sessions.configure(info={"file_store": FileStore(tmp_path / "workspace")})
    task = LearningTaskService(sessions).create(
        conversation_id=run["conversation_id"],
        request_key="delegated",
        title="事务学习",
        goal="理解事务",
        mode="delegated",
        units=[{"key": "one", "title": "原子性", "goal": "理解一组操作如何提交"}],
    )
    LearningTaskService(sessions).confirm(task["id"], expected_revision=1, auto_publish=True)
    with sessions.begin() as session:
        root = session.get(Run, context.root_id)
        root.learning_task_id, root.learning_unit_id = task["id"], task["units"][0]["id"]
        for role in ("reader", "reviewer"):
            definition = SubagentDefinition(
                id="learning-" + role,
                name="learning-" + role,
                display_name=role,
                description="固定草稿协作",
                system_prompt="独立核对，按合同返回JSON。",
                tools=["read_learning_draft"],
                resolved_tools=["read_learning_draft"],
                skills=[],
                enabled=True,
            )
            session.add(definition)
            SubagentRegistry._save(session, definition)
    ToolPolicyService(sessions).update(
        expected_revision=0,
        enabled={
            "builtin:learning_feedback": True,
            "builtin:read_learning_draft": True,
            "builtin:propose_learning_framework": True,
        },
    )
    draft = context.knowledge.create_draft(
        context.root_id,
        [{"action": "create", "title": "事务原子性", "body": "事务中的操作作为整体提交或回滚。"}],
    )
    return module, context, run, task, draft


def run_collaboration(fixture, *, publish=False):
    module, context, run, _, draft = fixture
    provider = CollaborationProvider(draft["id"], publish=publish)
    context.gateway.client = provider
    graph = module.build_agent(context, checkpointer=InMemorySaver())
    drive_graph(
        context, graph,
        {"messages": [HumanMessage(run["task"])]},
    )
    return provider


def test_real_children_read_entire_material_and_share_publication_gate(collaboration):
    _, context, run, task, draft = collaboration
    provider = run_collaboration(collaboration, publish=True)
    rows = LearningCollaborationService(context.jobs.sessions).list(task["id"])
    assert len(rows) == 2 and all(r["status"] == "completed" for r in rows)
    assert all(r["read_ranges"] == [[0, r["material_length"]]] for r in rows)
    assert len({r["child_run_id"] for r in rows}) == 2
    assert all(r["child_run_id"] != run["id"] for r in rows)
    assert context.knowledge.get_draft(draft["id"])["status"] == "published"
    assert provider.child_inputs
    with context.jobs.sessions() as session:
        unit = session.get(LearningUnit, task["units"][0]["id"])
        assert unit.status == "published" and unit.result_version_id
    count = len(context.gateway.ledger.records())
    replay = drive_delegation(context, lambda: learning_feedback.func(
        role="reviewer",
        changeset_id=draft["id"],
        expected_revision=1,
        runtime=SimpleNamespace(context=context, tool_call_id="review-call"),
    ), identity="review-replay")
    assert replay["id"] == rows[1]["id"] and len(context.gateway.ledger.records()) == count


def test_forged_or_stale_review_cannot_publish_but_exact_human_takeover_can(collaboration):
    _, context, _, task, draft = collaboration
    with pytest.raises(PermissionError, match="审读"):
        context.knowledge.publish(draft["id"], idempotency_key="early")
    run_collaboration(collaboration)
    service = LearningCollaborationService(context.jobs.sessions)
    row = service.list(task["id"])[1]
    with pytest.raises(PermissionError):
        service.complete(context, row["id"], context.root_id)
    changed = context.knowledge.edit_draft(
        draft["id"],
        [{**draft["operations"][0], "body": "这是后来改动的正文。"}],
        expected_revision=1,
    )
    with pytest.raises(PermissionError, match="草稿版本"):
        context.knowledge.publish(draft["id"], idempotency_key="stale")
    receipt = context.knowledge.publish(
        draft["id"],
        idempotency_key="human",
        expected_revision=changed["revision"],
        user_approval=True,
    )
    assert receipt["learning_publication"] == "human_approved"


def test_unread_material_never_counts_as_successful_reader(collaboration):
    module, context, _, task, draft = collaboration
    context.gateway.client = CollaborationProvider(draft["id"], skip_read=True)
    module.build_agent(context, checkpointer=InMemorySaver())
    for number in range(2):
        report = drive_delegation(context, lambda number=number: learning_feedback.func(
            role="reader",
            changeset_id=draft["id"],
            expected_revision=1,
            runtime=SimpleNamespace(context=context, tool_call_id=f"bad-{number}"),
        ), identity=f"invalid-reader-{number}")
        assert report["status"] == "invalid" and "完整" in report["error"]
    with context.jobs.sessions() as session:
        unit = session.get(LearningUnit, task["units"][0]["id"])
        assert unit.status == "blocked" and unit.reader_rounds == 2
        assert len(list(session.scalars(select(Run).where(Run.parent_id == context.root_id)))) == 2
    with pytest.raises(ValueError):
        learning_feedback.func(
            role="reader",
            changeset_id=draft["id"],
            expected_revision=1,
            runtime=SimpleNamespace(context=context, tool_call_id="over-budget"),
        )


def test_requirement_change_invalidates_completed_review(collaboration):
    _, context, _, task, draft = collaboration
    run_collaboration(collaboration)
    tasks = LearningTaskService(context.jobs.sessions)
    latest = tasks.get(task["id"])
    tasks.configure(
        task["id"],
        expected_revision=latest["revision"],
        changes={"examples": "需要新的SQL例子"},
        source={"kind": "manual"},
    )
    with pytest.raises(PermissionError, match="草稿版本|要求|审读"):
        context.knowledge.publish(draft["id"], idempotency_key="new-requirements")
    assert context.knowledge.get_draft(draft["id"])["status"] == "draft"


def test_general_file_sync_cannot_bypass_delegated_review(collaboration, tmp_path):
    from knowledgeops_agent.knowledge.files import synchronize
    from knowledgeops_agent.persistence.models import KnowledgeVersion

    _, context, _, _, _ = collaboration
    context.workspace_root = tmp_path / "workspace"
    path = "knowledge/documents/未经审读.md"
    store = FileStore(context.workspace_root)
    store.replace(path, "# 未经审读\n保留工作文件，不能直接变成正式版本。".encode())
    result = synchronize(context, [path], operation_key="file-bypass")
    assert result["status"] == "draft_ready"
    assert "审读" in result["reason"]
    assert store.path(path).exists()
    draft = context.knowledge.get_draft(result["changeset_id"])
    assert draft["status"] == "draft"
    with context.jobs.sessions() as session:
        assert list(session.scalars(select(KnowledgeVersion))) == []


@pytest.mark.parametrize("resource", ["role", "tool"])
def test_disabled_collaboration_resource_stops_before_model_call(collaboration, resource):
    from knowledgeops_agent.harness.runtime.resources import ResourceUnavailable

    module, context, _, _, _ = collaboration
    if resource == "role":
        registry = SubagentRegistry(context.jobs.sessions)
        current = registry.get("learning-reader")
        registry.update("learning-reader", expected_revision=current["revision"], enabled=False)
    else:
        policies = ToolPolicyService(context.jobs.sessions)
        current = policies.get()
        policies.update(
            expected_revision=current["revision"], enabled={"builtin:read_learning_draft": False}
        )
    with pytest.raises(ResourceUnavailable):
        module.build_agent(context, checkpointer=InMemorySaver())
    assert context.gateway.ledger.records() == []
