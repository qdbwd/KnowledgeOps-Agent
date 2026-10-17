"""不发送推理请求：真实 SDK 序列化、HTTP 元数据替身与能力拒绝边界。"""

import json

import httpx
import pytest
from langchain_core.messages import HumanMessage

from knowledgeops_agent.core.config import Settings, create_chat_client
from knowledgeops_agent.harness.models.reasoning import (
    ReasoningProfile,
    ReasoningService,
    reasoning_parameters,
)


def settings(**overrides):
    return Settings(
        _env_file=None,
        **{
            "model_calls_enabled": True,
            "openai_api_key": "fictional-metadata-key",
            "openai_base_url": "https://api.deepseek.com",
            "agent_model": "deepseek-flash",
            "model_provider": "deepseek",
            **overrides,
        },
    )


def test_metadata_controls_options_defaults_and_is_cached_without_generation():
    calls = []

    def metadata(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/models"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "deepseek-flash",
                        "effort": {
                            "supported_levels": ["low", "high"],
                            "default_level": "low",
                        },
                    }
                ]
            },
        )

    service = ReasoningService(settings(), transport=httpx.MockTransport(metadata))
    assert service.catalog()["levels"] == ["low", "high"]
    assert service.freeze()["effective"] == "low"
    assert service.freeze("off")["parameters"] == {"extra_body": {"thinking": {"type": "disabled"}}}
    with pytest.raises(ValueError, match="不支持"):
        service.freeze("max")
    assert len(calls) == 1
    assert "fictional-metadata-key" not in json.dumps(service.catalog())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="private provider error"),
        httpx.Response(200, json={"data": [{"id": "deepseek-flash"}]}),
        httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "deepseek-flash",
                        "effort": {"supported_levels": [], "default_level": None},
                    }
                ]
            },
        ),
    ],
)
def test_failed_or_invalid_metadata_uses_explicit_fallback(response):
    service = ReasoningService(settings(), transport=httpx.MockTransport(lambda _: response))
    result = service.catalog()
    assert result["source"] == "configuration" and result["default_level"] == "high"
    assert "private" not in json.dumps(result)


def test_offline_unknown_provider_never_invents_levels_or_probes():
    service = ReasoningService(settings(model_calls_enabled=False, agent_model="unknown"))
    assert service.catalog()["levels"] == []
    assert service.freeze()["parameters"] == {}
    with pytest.raises(ValueError, match="不支持"):
        service.freeze("high")


def test_real_sdk_payload_keeps_deepseek_effort_and_thinking_separate():
    service = ReasoningService(settings(model_calls_enabled=False))
    client = create_chat_client(settings())
    for selection in ("low", "high", "max", "off"):
        params = service.freeze(selection)["parameters"]
        payload = client._get_request_payload([HumanMessage("fixture")], **params)
        assert payload["extra_body"]["thinking"]["type"] == (
            "disabled" if selection == "off" else "enabled"
        )
        if selection == "off":
            assert "reasoning_effort" not in payload
        else:
            assert payload["reasoning_effort"] == selection


def test_openai_compatible_uses_its_declared_vocabulary_and_sdk():
    profile = ReasoningProfile(
        provider="openai-compatible",
        model="fixture",
        endpoint_host="models.example.test",
        levels=["minimal", "high"],
        default_level="minimal",
    )
    _, parameters = reasoning_parameters(profile, "default")
    client = create_chat_client(
        settings(
            model_provider="openai-compatible", openai_base_url="https://models.example.test/v1"
        )
    )
    payload = client._get_request_payload([HumanMessage("fixture")], **parameters)
    assert type(client).__name__ == "ChatOpenAI"
    assert payload["reasoning_effort"] == "minimal" and "extra_body" not in payload
    with pytest.raises(ValueError, match="关闭"):
        reasoning_parameters(profile, "off")


def test_official_client_emits_actual_http_body_without_a_network_call():
    bodies = []

    def completion(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "offline-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "fixture"},
                    }
                ],
            },
        )

    service = ReasoningService(settings(model_calls_enabled=False))
    with httpx.Client(transport=httpx.MockTransport(completion)) as http_client:
        client = create_chat_client(settings(), http_client=http_client)
        client.invoke([HumanMessage("fixture")], **service.freeze("max")["parameters"])
    assert bodies[0]["thinking"] == {"type": "enabled"}
    assert bodies[0]["reasoning_effort"] == "max"
