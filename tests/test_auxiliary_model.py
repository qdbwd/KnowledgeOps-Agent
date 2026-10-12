"""通用模型工具的隔离、固定材料、计费和恢复合同，供应商使用受控响应。"""

import importlib
import json

import pytest
from langchain_core.messages import AIMessage
from test_runtime import runtime_services as _runtime_services
from test_runtime import setup_run

runtime_services = _runtime_services


class CapturingProvider:
    def __init__(self):
        self.received = []

    def invoke(self, messages, **kwargs):
        self.received.append((messages, kwargs))
        return AIMessage(
            "材料表明：最多三次尝试，不保证成功。" * 900,
            response_metadata={"finish_reason": "stop"},
        )


def setup(runtime_services):
    _, context, _ = setup_run(runtime_services, auxiliary=True)
    provider = CapturingProvider()
    context.gateway.client = provider
    return importlib.import_module("knowledgeops_agent.auxiliary_model"), context, provider


def test_isolated_input_fixed_version_and_durable_result_replay(runtime_services):
    module, context, provider = setup(runtime_services)
    old = context.sources.import_text("限定材料", "最多三次尝试，不保证成功。", origin="aux-source")
    context.sources.import_text("限定材料", "新版改成五次。", origin="aux-source")
    args = {
        "prompt": "提取数值并保留否定。",
        "material_refs": [{"kind": "source_version", "id": old["version_id"]}],
        "tool_call_id": "aux-a",
    }
    result = module.call_auxiliary(context, **args)
    assert result["status"] == "succeeded" and result["truncated"]
    assert len(result["preview"]) <= 3000
    messages, parameters = provider.received[0]
    assert len(messages) == 2
    payload = json.loads(messages[-1].content)
    assert payload["prompt"] == args["prompt"]
    assert "最多三次" in payload["materials"][0]["text"]
    assert "新版改成五次" not in str(messages)
    assert "整理材料" not in str(messages)  # 主任务和主历史不自动泄漏
    assert "tools" not in parameters and "root_id" not in payload
    assert context.jobs.read_artifact(result["artifact_id"], root_id=context.root_id)["text"]
    assert module.call_auxiliary(context, **args) == result
    assert len(provider.received) == 1
    assert len(context.gateway.ledger.records()) == 1
    module.call_auxiliary(context, **{**args, "tool_call_id": "aux-b"})
    assert len(provider.received) == 2  # 不同逻辑调用不靠提示词全局去重


def test_reference_scope_and_input_conflict_do_not_make_new_model_request(runtime_services):
    from knowledgeops_agent.model_gateway import ModelReplayConflict

    module, context, provider = setup(runtime_services)
    other = context.jobs.enqueue(
        context.jobs.create_conversation()["id"], "其他任务", request_key="other-aux"
    )
    artifact = context.jobs.save_artifact(other["id"], "tool_result", "不可跨任务读取")
    with pytest.raises(PermissionError):
        module.call_auxiliary(context, "读取", [{"kind": "artifact", "id": artifact}], "bad")
    with pytest.raises(ValueError):
        module.call_auxiliary(
            context, "读取", [{"kind": "source_version", "id": "/etc/passwd"}], "bad"
        )
    assert provider.received == []
    module.call_auxiliary(context, "原任务", [], "stable-id")
    with pytest.raises(ModelReplayConflict):
        module.call_auxiliary(context, "修改后的任务", [], "stable-id")
    assert len(provider.received) == 1


def test_partial_result_is_retained_but_not_reported_as_complete(runtime_services):
    module, context, _ = setup(runtime_services)

    class Truncated:
        def invoke(self, messages, **kwargs):
            return AIMessage("未写完", response_metadata={"finish_reason": "length"})

    context.gateway.client = Truncated()
    result = module.call_auxiliary(context, "任务", [], "short")
    assert result["status"] == "incomplete"
    assert result["finish_reason"] == "length"
    assert result["artifact_id"]


def test_draft_reference_is_exact_revision(runtime_services):
    module, context, provider = setup(runtime_services)
    draft = context.knowledge.create_draft(
        context.root_id, [{"action": "create", "title": "旧草稿", "body": "原修订内容"}]
    )
    context.knowledge.edit_draft(
        draft["id"],
        [{"action": "create", "title": "新草稿", "body": "新修订内容"}],
        expected_revision=1,
    )
    module.call_auxiliary(
        context, "核对旧版", [{"kind": "draft_revision", "id": draft["id"], "revision": 1}], "old"
    )
    body = str(provider.received[0][0])
    assert "原修订内容" in body and "新修订内容" not in body
