"""完整请求容量在调用供应商和费用预占之前检查，不能只数历史正文。"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from test_model_gateway import ledger as _ledger

from knowledgeops_agent.model_gateway import ModelGateway

ledger = _ledger


def profile():
    from knowledgeops_agent.request_capacity import ModelCapacity

    return ModelCapacity(
        context_window=8192,
        max_output_tokens=2048,
        working_window=4096,
        output_reserve=256,
        framing_reserve=128,
    )


class Provider:
    def invoke(self, messages, **kwargs):
        return AIMessage("ok")


@pytest.mark.parametrize("large_part", ["system", "tools", "protocol"])
def test_complete_request_rejected_without_charge(ledger, large_part):
    from knowledgeops_agent.request_capacity import ContextCapacityExceeded

    messages = [SystemMessage("small"), HumanMessage("任务")]
    tools = []
    if large_part == "system":
        messages[0] = SystemMessage("规则" * 6000)
    elif large_part == "tools":
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "schema" * 6000,
                    "parameters": {"type": "object"},
                },
            }
        ]
    else:
        messages.insert(
            1, AIMessage("done", additional_kwargs={"reasoning_content": "思考" * 6000})
        )
    gateway = ModelGateway(ledger, enabled=True, client=Provider(), capacity=profile())
    with pytest.raises(ContextCapacityExceeded, match="request capacity"):
        gateway.invoke(messages, root_id="root", run_id="run", tools=tools)
    assert ledger.records() == []


def test_output_reserve_and_capacity_evidence(ledger):
    gateway = ModelGateway(ledger, enabled=True, client=Provider(), capacity=profile())
    gateway.invoke([HumanMessage("材料")], root_id="root", run_id="run")
    evidence = ledger.records()[0]["request"]
    assert evidence["parameters"]["max_tokens"] == 256
    assert evidence["capacity"]["estimated"] is True
    assert evidence["capacity"]["output_tokens"] == 256
    assert evidence["capacity"]["input_tokens"] > len("材料")
    from knowledgeops_agent.request_capacity import ContextCapacityExceeded

    with pytest.raises(ContextCapacityExceeded, match="output"):
        gateway.invoke([HumanMessage("short")], root_id="root", run_id="run", max_tokens=3000)
    assert len(ledger.records()) == 1


def test_final_provider_serializer_is_counted(ledger):
    from knowledgeops_agent.request_capacity import ContextCapacityExceeded

    class ExtendedProvider(Provider):
        def _get_request_payload(self, messages, **kwargs):
            return {"messages": [{"role": "system", "content": "injected" * 6000}], **kwargs}

    gateway = ModelGateway(ledger, enabled=True, client=ExtendedProvider(), capacity=profile())
    with pytest.raises(ContextCapacityExceeded):
        gateway.invoke([HumanMessage("short")], root_id="root", run_id="run")
    assert ledger.records() == []


def test_profile_is_bound_to_the_verified_endpoint():
    from knowledgeops_agent.request_capacity import load_model_capacity

    verified = load_model_capacity("deepseek-flash", "https://api.deepseek.com/v1")
    assert verified.context_window == 1048576
    assert verified.working_window == 262144
    assert verified.summary_fraction == 0.9
    assert verified.max_output_tokens == 393216
    with pytest.raises(ValueError, match="profile"):
        load_model_capacity("deepseek-flash", "https://other-provider.invalid/v1")


def test_official_tokenizer_counts_model_prompt_instead_of_utf8_bytes():
    from knowledgeops_agent.request_capacity import ModelCapacity

    payload = {
        "model": "deepseek-flash",
        "messages": [
            {"role": "system", "content": "你是知识助手。"},
            {"role": "user", "content": "请解释消息上下文。"},
        ],
        "thinking": {"type": "enabled"},
    }
    measured = ModelCapacity().measure(payload)
    # 来自官方 V4.1 模板和固定 tokenizer 的离线样例；不是供应商账单。
    assert measured["input_tokens"] == 39
    assert measured["method"] == "deepseek_v41_tokenizer_v1"
    assert measured["estimated"] is True
    assert measured["total_tokens"] == 39 + 8192 + 4096


def test_frozen_old_capacity_retains_original_counter():
    from knowledgeops_agent.request_capacity import ModelCapacity

    original = profile().model_dump(exclude={"token_counter"})
    restored = ModelCapacity.from_snapshot(original)
    assert restored.working_window == 4096
    assert restored.measure({"messages": []})["method"] == "utf8_bytes_plus_framing_v1"
    new_profile = ModelCapacity.from_snapshot(ModelCapacity().model_dump())
    assert new_profile.token_counter == "deepseek_v41_tokenizer_v1"
