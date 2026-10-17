"""按任务选择辅助模型；实际业务库和构图边界，供应商仅用离线替身。"""

import importlib
from dataclasses import replace

import pytest
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

from knowledgeops_agent.persistence.models import Run

runtime_services = _runtime_services


def context_with_choice(runtime_services, choice):
    module, context, run = setup_run(runtime_services)
    with context.jobs.sessions.begin() as session:
        row = session.get(Run, run["id"])
        inputs = dict(row.input_data)
        if choice is None:
            inputs.pop("auxiliary", None)  # 升级前的真实记录可以没有新字段。
        else:
            inputs["auxiliary"] = choice
        row.input_data = inputs
    from knowledgeops_agent.harness.runtime.resources import prepare_resources

    prepare_resources(context)
    return module, context


@pytest.mark.parametrize("choice", [None, False, True])
@pytest.mark.parametrize("child", [False, True])
def test_auxiliary_registry_uses_root_choice(runtime_services, monkeypatch, choice, child):
    module, context = context_with_choice(runtime_services, choice)
    if child:
        row = context.jobs.start_child(context.root_id, "只读核对")
        assert context.jobs.claim(context.worker_id, run_id=row["id"], generation=row["generation"])
        context = replace(context, run_id=row["id"])
        # 子记录不能以自身值扩大或改变根选择。
        with context.jobs.sessions.begin() as session:
            stored = session.get(Run, row["id"])
            stored.input_data = {**stored.input_data, "auxiliary": not bool(choice)}
    captured = {}

    def assemble(*args, **kwargs):
        captured["tools"] = {tool.name for tool in kwargs["tools"]}

    monkeypatch.setattr(module, "create_agent", assemble)
    module.build_agent(context, checkpointer=None)
    assert ("call_model" in captured["tools"]) is (choice is True)
    assert context.gateway.ledger.records() == []


@pytest.mark.parametrize("choice", [None, False])
@pytest.mark.parametrize("child", [False, True])
def test_disabled_auxiliary_service_rejects_before_artifacts_or_charge(
    runtime_services, choice, child
):
    from test_auxiliary_model import CapturingProvider

    _, context = context_with_choice(runtime_services, choice)
    if child:
        row = context.jobs.start_child(context.root_id, "只读核对")
        assert context.jobs.claim(context.worker_id, run_id=row["id"], generation=row["generation"])
        context = replace(context, run_id=row["id"])
        with context.jobs.sessions.begin() as session:
            stored = session.get(Run, row["id"])
            stored.input_data = {**stored.input_data, "auxiliary": True}
    provider = CapturingProvider()
    context.gateway.client = provider
    module = importlib.import_module("knowledgeops_agent.harness.models.auxiliary")
    before = context.jobs.events(context.run_id)
    with pytest.raises(PermissionError, match="auxiliary.*disabled"):
        module.call_auxiliary(context, "核对材料", [], "disabled-tool")
    assert provider.received == []
    assert context.gateway.ledger.records() == []
    assert context.jobs.artifacts(context.run_id) == []
    assert context.jobs.events(context.run_id) == before


def test_enabled_auxiliary_child_inherits_root_choice_and_replays(runtime_services):
    from test_auxiliary_model import CapturingProvider

    _, context = context_with_choice(runtime_services, True)
    row = context.jobs.start_child(context.root_id, "核对指定材料")
    assert row["input"]["auxiliary"] is True
    assert context.jobs.claim(context.worker_id, run_id=row["id"], generation=row["generation"])
    context = replace(context, run_id=row["id"])
    provider = CapturingProvider()
    context.gateway.client = provider
    module = importlib.import_module("knowledgeops_agent.harness.models.auxiliary")
    result = module.call_auxiliary(context, "列出材料不足之处", [], "child-aux")
    assert module.call_auxiliary(context, "列出材料不足之处", [], "child-aux") == result
    records = context.gateway.ledger.records(root_id=context.root_id)
    assert len(records) == 1
    assert records[0]["run_id"] == context.run_id
    assert records[0]["purpose"] == "auxiliary"
