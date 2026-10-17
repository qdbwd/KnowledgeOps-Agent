"""对照DeerFlow固定版本，验证真实SDK发送形状及消息数变化的回传合同。"""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_deepseek import ChatDeepSeek

from knowledgeops_agent.harness.models.deepseek import DeepSeekClient


def client(**kwargs):
    return DeepSeekClient(model="deepseek-flash", api_key="fictional-test-secret", **kwargs)


@pytest.mark.parametrize("thinking", ["enabled", "disabled"])
def test_tool_assistant_uses_string_content_and_only_explicit_thinking_gets_placeholder(thinking):
    model = client(extra_body={"thinking": {"type": thinking}})
    messages = [
        HumanMessage("查询"),
        AIMessage(content="", tool_calls=[{"id": "call-1", "name": "lookup", "args": {}}]),
        ToolMessage("结果", tool_call_id="call-1"),
    ]
    result = model._get_request_payload(messages)["messages"][1]
    assert result["content"] == ""
    if thinking == "enabled":
        assert result["reasoning_content"] == ""
    else:
        assert "reasoning_content" not in result


def test_per_request_disabled_thinking_overrides_enabled_client_without_faking_history():
    model = client(extra_body={"thinking": {"type": "enabled"}})
    message = AIMessage(content="", tool_calls=[{"id": "call-1", "name": "lookup", "args": {}}])
    result = model._get_request_payload([message], extra_body={"thinking": {"type": "disabled"}})
    assert "reasoning_content" not in result["messages"][0]


def test_assistant_replay_matches_actual_message_when_serializer_adds_or_drops_items(monkeypatch):
    """供应商序列化的assistant条目不依赖所有角色消息一一zip。"""
    model = client()
    messages = [
        HumanMessage("问题"),
        AIMessage("旧答", additional_kwargs={"reasoning_content": "old"}),
        AIMessage("新答", additional_kwargs={"reasoning_content": "new"}),
    ]
    monkeypatch.setattr(
        ChatDeepSeek,
        "_get_request_payload",
        lambda *args, **kwargs: {
            "messages": [
                {"role": "system", "content": "插入的控制信息"},
                {"role": "assistant", "content": "新答"},
            ]
        },
    )
    result = model._get_request_payload(messages)
    assert result["messages"][1]["reasoning_content"] == "new"
    assert "reasoning_content" not in result["messages"][0]


def test_reasoning_survives_langchain_message_checkpoint_round_trip():
    from langchain_core.messages import messages_from_dict, messages_to_dict

    message = AIMessage("答复", additional_kwargs={"reasoning_content": "opaque-real-value"})
    restored = messages_from_dict(json.loads(json.dumps(messages_to_dict([message]))))
    result = client()._get_request_payload(restored)
    assert result["messages"][0]["reasoning_content"] == "opaque-real-value"
    assert "fictional-test-secret" not in json.dumps(client().to_json(), default=str)
