"""辅助调用恢复以真实调用ID关联，不靠提示词的全局哈希缓存。"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from test_model_gateway import ledger as _ledger

from knowledgeops_agent.harness.models.gateway import ModelGateway

ledger = _ledger


class Provider:
    def __init__(self):
        self.calls = 0

    def invoke(self, messages, **kwargs):
        self.calls += 1
        return AIMessage("已提取事实", additional_kwargs={"reasoning_content": "opaque"})


def test_successful_recovery_reuses_result_but_new_call_id_is_charged(ledger):
    provider = Provider()
    gateway = ModelGateway(ledger, enabled=True, client=provider)
    args = dict(root_id="root", run_id="run", purpose="auxiliary", call_id="tool-a")
    first = gateway.invoke([HumanMessage("extract")], **args)
    reopened = ModelGateway(ledger, enabled=True, client=provider)
    second = reopened.invoke([HumanMessage("extract")], **args)
    assert second == first
    assert provider.calls == 1
    reopened.invoke([HumanMessage("extract")], **{**args, "call_id": "tool-b"})
    assert provider.calls == 2
    assert len(ledger.records()) == 2


def test_changed_input_same_id_is_rejected_before_a_new_request(ledger):
    from knowledgeops_agent.harness.models.gateway import ModelReplayConflict

    provider = Provider()
    gateway = ModelGateway(ledger, enabled=True, client=provider)
    gateway.invoke([HumanMessage("one")], root_id="root", run_id="run", call_id="same")
    with pytest.raises(ModelReplayConflict):
        gateway.invoke([HumanMessage("two")], root_id="root", run_id="run", call_id="same")
    assert provider.calls == 1 and len(ledger.records()) == 1


def test_unknown_attempt_stays_charged_and_is_not_automatically_retried(ledger):
    from knowledgeops_agent.harness.models.gateway import ModelCallUncertain

    class CrashProvider(Provider):
        def invoke(self, messages, **kwargs):
            raise SystemExit("process stopped after request was sent")

    args = dict(root_id="root", run_id="run", purpose="auxiliary", call_id="pending")
    with pytest.raises(SystemExit):
        ModelGateway(ledger, enabled=True, client=CrashProvider()).invoke(
            [HumanMessage("x")], **args
        )
    provider = Provider()
    with pytest.raises(ModelCallUncertain):
        ModelGateway(ledger, enabled=True, client=provider).invoke([HumanMessage("x")], **args)
    assert provider.calls == 0
    assert len(ledger.records()) == 1
    assert ledger.records()[0]["status"] == "reserved"


def test_distinct_runs_do_not_share_call_identity(ledger):
    provider = Provider()
    gateway = ModelGateway(ledger, enabled=True, client=provider)
    for run in ("one", "two"):
        gateway.invoke([HumanMessage("same")], root_id="root", run_id=run, call_id="same")
    assert provider.calls == 2
