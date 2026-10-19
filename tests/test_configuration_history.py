"""旧配置只回收无实际依赖的修订，不破坏冻结执行与业务审读。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import delete, select, text
from sqlalchemy.exc import OperationalError
from test_learning_collaboration import collaboration as _collaboration
from test_lifecycle_details import checkpoint_case as _checkpoint_case
from test_lifecycle_details import detail_case as _detail_case
from test_lifecycle_details import ledger as _ledger
from test_lifecycle_details import redis_case as _redis_case
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run
from test_subagent_registry import fields
from test_subagent_runtime import role

from knowledgeops_agent.app.lifecycle.configuration_history import (
    ConfigurationHistoryCleaner,
    configuration_history_batch,
)
from knowledgeops_agent.app.lifecycle.maintenance import SCAN_STATE, LifecycleMaintenance
from knowledgeops_agent.harness.subagents.registry import SubagentRegistry
from knowledgeops_agent.harness.tools.configuration import lock_configuration
from knowledgeops_agent.harness.tools.groups import ToolGroupService
from knowledgeops_agent.harness.tools.policy import ToolPolicyService
from knowledgeops_agent.learning.collaboration import LearningCollaborationService
from knowledgeops_agent.persistence import models as db

detail_case, ledger, checkpoint_case, redis_case = _detail_case, _ledger, _checkpoint_case, _redis_case
runtime_services, collaboration = _runtime_services, _collaboration


def test_scanner_prunes_old_unreferenced_policy_in_bounded_resumable_batches(detail_case):
    details, _, _, _, clock, _, _, _, _ = detail_case
    policies = ToolPolicyService(details.sessions)
    for revision in range(4):
        policies.update(expected_revision=revision)
    old = clock[0] - timedelta(days=31)
    with details.sessions.begin() as session:
        for version in session.scalars(select(db.ToolPolicyRevision)):
            version.created_at = old
        session.add(db.MaintenanceState(name=SCAN_STATE,
            cursor={"phase": "configuration", "kind": "tool_policy"},
            next_scan_at=clock[0], updated_at=clock[0]))
    first = LifecycleMaintenance(details).scan_step()
    assert first["error"] is None, first
    assert first["removed"] == 2
    with details.sessions() as session:
        assert session.get(db.ToolPolicyRevision, ("default", 4)) is not None
        assert session.get(db.MaintenanceState, SCAN_STATE).cursor["after"] == ["default", 1]
    second = LifecycleMaintenance(details).scan_step()
    assert second["error"] is None and second["removed"] == 2
    third = LifecycleMaintenance(details).scan_step()
    assert third["error"] is None and third["removed"] == 0
    assert policies.history() == [policies.get()]


@pytest.mark.parametrize("kind", ["subagent", "tool_policy", "tool_group"])
def test_preview_does_not_delete_and_current_recent_and_tombstone_revisions_survive(business_sessions, kind):
    if kind == "subagent":
        service = SubagentRegistry(business_sessions)
        current = service.create(**fields(skills=[]))
        model = db.SubagentDefinitionRevision

        def update(revision):
            return service.update(current["id"], expected_revision=revision - 1,
                                  system_prompt=f"第{revision}次规则", deleted=revision == 4)
    elif kind == "tool_policy":
        service = ToolPolicyService(business_sessions)
        current = service.update(expected_revision=0)
        model = db.ToolPolicyRevision

        def update(revision):
            return service.update(expected_revision=revision - 1)
    else:
        service = ToolGroupService(business_sessions)
        current = service.create(name="资料组", tool_ids=["builtin:read_source"])
        model = db.ToolGroupRevision

        def update(revision):
            changes = {"name": f"资料组{revision}", "deleted": revision == 4}
            preview = service.preview(current["id"], expected_revision=revision - 1, **changes)
            return service.update(current["id"], expected_revision=revision - 1,
                                  preview_token=preview["preview_token"], **changes)
    for revision in (2, 3, 4):
        current = update(revision)
    now = db.utcnow()
    with business_sessions.begin() as session:
        for version in session.scalars(select(model)):
            version.created_at = now - timedelta(days=1 if version.revision == 3 else 31)
    cleaner = ConfigurationHistoryCleaner(business_sessions, now=lambda: now)
    preview = cleaner.preview(kind)
    assert preview["removed"] == 0
    assert {item["reason"] for item in preview["items"]} == {"current", "retention_period", "unreferenced"}
    with business_sessions() as session:
        assert session.get(model, (current["id"], 1)) is not None
    result = cleaner.step(kind)
    assert result["removed"] == preview["eligible"]
    with business_sessions() as session:
        assert session.get(model, (current["id"], 1)) is None
        assert session.get(model, (current["id"], 3)) is not None
        assert session.get(model, (current["id"], 4)) is not None


def test_frozen_graph_resumes_with_old_roles_and_policy_after_unrelated_revisions_are_pruned(runtime_services):
    module, context, run = setup_run(runtime_services)
    sessions = context.jobs.sessions
    frozen_role = role(context)
    policies = ToolPolicyService(sessions)
    frozen_policy = policies.update(expected_revision=0)
    saver = InMemorySaver()
    graph = module.build_agent(context, checkpointer=saver)
    config = {"configurable": {"thread_id": run["checkpoint_id"]}}
    graph.invoke({"messages": module.initial_messages(context)}, config, context=context, interrupt_before=["model"])
    assert graph.get_state(config).next
    registry = SubagentRegistry(sessions)
    for revision in (2, 3):
        registry.update(frozen_role["id"], expected_revision=revision - 1, system_prompt=f"后来编辑{revision}")
        policies.update(expected_revision=revision - 1)
    cleaner = ConfigurationHistoryCleaner(sessions, now=lambda: db.utcnow() + timedelta(days=31))
    assert cleaner.step("subagent")["removed"] == 1
    assert cleaner.step("tool_policy")["removed"] == 2  # 无依赖的0、2；冻结1和当前3保留。
    assert registry.validate_binding(frozen_role) == frozen_role
    assert "builtin:read_source" in policies.allowed_ids(frozen_policy, is_root=True)
    reopened = module.build_agent(context, checkpointer=saver)
    state = reopened.invoke(None, config, context=context)
    assert not reopened.get_state(config).next
    assert state["messages"][-1].text and context.knowledge.list_drafts(root_id=context.root_id)
    with sessions() as session:
        assert session.get(db.RunResourceSnapshot, context.root_id).content["tool_policy"] == frozen_policy


def test_child_binding_protects_its_revision_until_the_binding_is_released(runtime_services):
    _, context, _ = setup_run(runtime_services)
    sessions = context.jobs.sessions
    frozen = role(context)
    child = context.jobs.start_child(context.root_id, "读取材料", operation_key="retained-child", subagent=frozen)
    SubagentRegistry(sessions).update(frozen["id"], expected_revision=1, system_prompt="修改后的规则")
    cleaner = ConfigurationHistoryCleaner(sessions, now=lambda: db.utcnow() + timedelta(days=31))
    assert cleaner.step("subagent")["removed"] == 0
    with sessions.begin() as session:
        row = session.get(db.Run, child["id"])
        row.lifecycle_state, row.input_data = "expired", {}
    assert cleaner.step("subagent")["removed"] == 1


def test_learning_report_keeps_role_revision_even_after_runtime_resources_are_gone(collaboration):
    module, context, _, _, draft = collaboration
    module.build_agent(context, checkpointer=InMemorySaver())
    report = LearningCollaborationService(context.jobs.sessions).prepare(context, "reader", draft["id"], 1, "retained-report")
    registry = SubagentRegistry(context.jobs.sessions)
    registry.update("learning-reader", expected_revision=1, system_prompt="后来修改的读者规则")
    with context.jobs.sessions.begin() as session:
        session.execute(delete(db.RunResourceSnapshot).where(db.RunResourceSnapshot.root_id == context.root_id))
    cleaner = ConfigurationHistoryCleaner(context.jobs.sessions, now=lambda: db.utcnow() + timedelta(days=31))
    assert cleaner.step("subagent")["removed"] == 0
    assert registry.validate_binding(report["definition"]) == report["definition"]
    with context.jobs.sessions.begin() as session:
        session.delete(session.get(db.LearningCollaboration, report["id"]))
    assert cleaner.step("subagent")["removed"] == 1


def test_failed_commit_preserves_old_versions_and_replay_prunes_once(business_sessions):
    policies = ToolPolicyService(business_sessions)
    for revision in range(3):
        policies.update(expected_revision=revision)
    now = db.utcnow() + timedelta(days=31)
    with pytest.raises(RuntimeError, match="commit failed"):
        with business_sessions.begin() as session:
            assert configuration_history_batch(session, "tool_policy", now=now, execute=True)["removed"] == 3
            raise RuntimeError("commit failed")
    assert len(policies.history()) == 4
    cleaner = ConfigurationHistoryCleaner(business_sessions, now=lambda: now)
    assert cleaner.step("tool_policy")["removed"] == 3
    assert cleaner.step("tool_policy")["removed"] == 0


@pytest.mark.parametrize("shared", [True, False])
def test_cleanup_waits_for_configuration_freeze_or_edit_transaction(business_sessions, shared):
    """用真实PG锁超时证明互斥，避免凭线程是否刚好结束来猜测锁已生效。"""
    policies = ToolPolicyService(business_sessions)
    for revision in range(2):
        policies.update(expected_revision=revision)
    now = db.utcnow() + timedelta(days=31)

    def competing_cleanup():
        with business_sessions.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '250ms'"))
            return configuration_history_batch(session, "tool_policy", now=now, execute=True)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with business_sessions.begin() as session:
            lock_configuration(session, shared=shared)
            future = pool.submit(competing_cleanup)
            with pytest.raises(OperationalError) as failure:
                future.result(timeout=5)
            assert failure.value.orig.sqlstate == "55P03"
            assert session.get(db.ToolPolicyRevision, ("default", 0)) is not None
    assert ConfigurationHistoryCleaner(business_sessions, now=lambda: now).step("tool_policy")["removed"] == 2
