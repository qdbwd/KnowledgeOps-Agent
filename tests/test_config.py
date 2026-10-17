"""配置错误应在发送模型请求之前暴露，repr 不包含用户密钥。"""

import importlib
from pathlib import Path

import pytest
from pydantic import ValidationError


def test_model_switch_comes_from_env_file_and_process_override(tmp_path, monkeypatch):
    """只验证配置装载，不建立模型客户端或发送请求。"""
    config = importlib.import_module("knowledgeops_agent.core.config")
    path = tmp_path / ".env"
    path.write_text("MODEL_CALLS_ENABLED=true\n", encoding="utf-8")
    monkeypatch.delenv("MODEL_CALLS_ENABLED", raising=False)
    credentials = dict(
        openai_api_key="fictional-config-key",
        openai_base_url="https://models.example.test/v1",
    )
    assert config.Settings(_env_file=path, **credentials).model_calls_enabled is True
    monkeypatch.setenv("MODEL_CALLS_ENABLED", "false")
    assert config.Settings(_env_file=path, **credentials).model_calls_enabled is False


def test_operational_settings_from_file_reach_model_client(tmp_path, monkeypatch):
    """用非默认值核对实际 dotenv 读取、类型转换和 SDK 参数，不发送模型请求。"""
    config = importlib.import_module("knowledgeops_agent.core.config")
    expected = {
        "model_calls_enabled": False,
        "agent_model": "offline-config-probe",
        "openai_base_url": "https://models.example.test/v1",
        "model_timeout_seconds": 73.0,
        "api_allowed_hosts": "localhost,config.example.test",
        "app_log_level": "error",
        "workspace_execute_timeout": 91,
        "runtime_redis_url": "redis://config.example.test:6379/2",
        "redis_events_enabled": False,
        "redis_checkpoint_cache_enabled": False,
        "redis_event_max_entries": 333,
        "redis_event_ttl_seconds": 600,
        "redis_checkpoint_ttl_seconds": 120,
        "redis_checkpoint_max_entries": 44,
        "redis_checkpoint_max_value_bytes": 65536,
        "mcp_session_max_connections": 7,
        "mcp_session_idle_seconds": 123.0,
        "mcp_session_sweep_seconds": 3.0,
        "mcp_session_close_seconds": 4.0,
    }
    path = tmp_path / ".env"
    lines = [f"{name.upper()}={str(value).lower() if isinstance(value, bool) else value}"
             for name, value in expected.items()]
    lines.append("OPENAI_API_KEY=fictional-config-probe")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for name in [*expected, "openai_api_key"]:
        monkeypatch.delenv(name.upper(), raising=False)
    settings = config.Settings(_env_file=path)
    for name, value in expected.items():
        assert getattr(settings, name) == value
    assert settings.openai_api_key.get_secret_value() == "fictional-config-probe"
    client = config.create_chat_client(settings)
    assert client.model_name == expected["agent_model"]
    assert client.request_timeout == expected["model_timeout_seconds"]
    assert client.openai_api_base == expected["openai_base_url"]


def test_public_env_template_does_not_offer_compose_owned_paths():
    """内部挂载位置由 Compose/备份合同约定，不能伪装成改了却无效的公开选项。"""
    from dotenv import dotenv_values

    values = dotenv_values(Path(__file__).resolve().parents[1] / ".env.example")
    assert {"WORKSPACE_ROOT", "SOURCE_STORAGE_ROOT", "KNOWLEDGE_BASE_ROOT"}.isdisjoint(values)
    assert values["OPENAI_API_KEY"] == ""
    assert values["POSTGRES_PASSWORD"] == ""


def test_lifecycle_settings_are_loaded_from_dotenv_with_explicit_override(tmp_path, monkeypatch):
    config = importlib.import_module("knowledgeops_agent.core.config")
    for field in config.Settings.model_fields:
        if field.startswith("lifecycle_"):
            monkeypatch.delenv(field.upper(), raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "LIFECYCLE_CLEANUP_ENABLED=false\nLIFECYCLE_RETENTION_DAYS=45\n"
        "LIFECYCLE_SCAN_INTERVAL_SECONDS=120\nLIFECYCLE_BATCH_SIZE=7\n"
        "LIFECYCLE_TEMPORARY_GRACE_SECONDS=7200\nLIFECYCLE_JOB_RETENTION_DAYS=9\n"
        "LIFECYCLE_JOB_LEASE_SECONDS=40\nLIFECYCLE_RETRY_SECONDS=12\n", encoding="utf-8",
    )
    monkeypatch.setenv("LIFECYCLE_BATCH_SIZE", "3")
    settings = config.Settings(_env_file=path, model_calls_enabled=False)
    assert settings.lifecycle_cleanup_enabled is False
    assert settings.lifecycle_retention_days == 45
    assert settings.lifecycle_scan_interval_seconds == 120
    assert settings.lifecycle_batch_size == 3
    assert settings.lifecycle_temporary_grace_seconds == 7200
    assert settings.lifecycle_job_retention_days == 9
    assert settings.lifecycle_job_lease_seconds == 40
    assert settings.lifecycle_retry_seconds == 12


def test_invalid_application_log_level_fails_before_startup():
    config = importlib.import_module("knowledgeops_agent.core.config")
    with pytest.raises(ValidationError, match="app_log_level"):
        config.Settings(_env_file=None, model_calls_enabled=False, app_log_level="verbose")


def test_online_config_requires_credentials_without_leaking_them(monkeypatch):
    config = importlib.import_module("knowledgeops_agent.core.config")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        config.Settings(_env_file=None, model_calls_enabled=True)
    settings = config.Settings(
        _env_file=None,
        model_calls_enabled=True,
        openai_api_key="fictional-config-key",
        openai_base_url="https://models.example.test/v1",
    )
    assert "fictional-config-key" not in repr(settings)
    assert settings.agent_model == "deepseek-flash"


def test_invalid_online_settings_do_not_expose_credentials(monkeypatch):
    config = importlib.import_module("knowledgeops_agent.core.config")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@db/test")
    monkeypatch.setenv("ACCOUNTING_DATABASE_URL", "postgresql://test:test@db/accounting")
    with pytest.raises(ValidationError) as caught:
        config.Settings(
            _env_file=None,
            model_calls_enabled=True,
            openai_api_key="fictional-invalid-config-secret",
            openai_base_url="https://models.example.test/v1",
            agent_model="",
        )
    assert "fictional-invalid-config-secret" not in str(caught.value)
    assert "input_value=" not in str(caught.value)


def test_provider_preserves_opaque_reasoning_protocol_for_followup():
    """只用本地 HTTP 替身：流式接收字段、合并、再次传回，不能靠填空串伪造。"""
    import json

    import httpx
    from langchain_core.messages import HumanMessage, message_chunk_to_message

    config = importlib.import_module("knowledgeops_agent.core.config")
    seen = []

    def respond(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if payload.get("stream"):
            chunks = [
                {
                    "id": "fixed",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "deepseek-flash",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }
                for delta in (
                    {"role": "assistant", "reasoning_content": "opaque-protocol-a"},
                    {"content": "可见答案"},
                )
            ]
            body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
            return httpx.Response(
                200, text=body + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(
            200,
            json={
                "id": "fixed2",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "修订答案",
                            "reasoning_content": "opaque-protocol-b",
                        },
                    }
                ],
            },
        )

    settings = config.Settings(
        _env_file=None,
        openai_api_key="fictional-key",
        openai_base_url="https://models.example.test/v1",
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        client = config.create_chat_client(settings, http_client=http_client).bind_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "read",
                        "description": "read",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
        )
        messages = [HumanMessage("测试")]
        chunks = list(client.stream(messages))
        combined = chunks[0]
        for chunk in chunks[1:]:
            combined += chunk
        response = message_chunk_to_message(combined)
        assert response.additional_kwargs["reasoning_content"] == "opaque-protocol-a"
        second = client.invoke([*messages, response])
        assert seen[1]["messages"][-1]["reasoning_content"] == "opaque-protocol-a"
        assert second.additional_kwargs["reasoning_content"] == "opaque-protocol-b"


@pytest.mark.parametrize("stream_tool_turn", [False, True])
def test_provider_preserves_reasoning_across_real_tool_roundtrip(stream_tool_turn):
    """在真实 SDK 的 HTTP/SSE 边界验证工具轮、结果回传与下一用户轮。

    普通和分片响应都带实际 tool_calls；协议字段来自替身响应，包含中文和换行，
    必须逐字回传，不能只验证字段存在或用空串占位。末轮无字段时也不能补造。
    """
    import json

    import httpx
    from langchain_core.messages import HumanMessage, ToolMessage, message_chunk_to_message

    config = importlib.import_module("knowledgeops_agent.core.config")
    tool_reasoning = "opaque-工具轮\n原始片段-a+片段-b"
    final_reasoning = "opaque-最终回复轮\n原样保留"
    seen = []
    tool_call = {
        "id": "call_read_segment",
        "type": "function",
        "function": {"name": "read", "arguments": '{"segment_id":"sample"}'},
    }

    def respond(request):
        payload = json.loads(request.content)
        seen.append(payload)
        turn = len(seen)
        if turn == 1 and stream_tool_turn:
            # 工具参数和协议文本分别跨块到达，必须经 SDK 合并为可回传的完整消息。
            deltas = [
                {"role": "assistant", "reasoning_content": "opaque-工具轮\n原始片段-a"},
                {"reasoning_content": "+片段-b"},
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": tool_call["id"],
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"segment_'},
                        }
                    ]
                },
                {"tool_calls": [{"index": 0, "function": {"arguments": 'id":"sample"}'}}]},
            ]
            chunks = [
                {
                    "id": "tool-turn",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "deepseek-flash",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }
                for delta in deltas
            ]
            chunks.append(
                {
                    "id": "tool-turn",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "deepseek-flash",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                }
            )
            body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
            return httpx.Response(
                200, text=body + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
            )
        message = {"role": "assistant", "content": "后续回复"}
        if turn == 1:
            message.update(content="", reasoning_content=tool_reasoning, tool_calls=[tool_call])
        elif turn == 2:
            message.update(content="工具读取后的答案", reasoning_content=final_reasoning)
        return httpx.Response(
            200,
            json={
                "id": f"turn-{turn}",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls" if turn == 1 else "stop",
                        "message": message,
                    }
                ],
            },
        )

    settings = config.Settings(
        _env_file=None,
        openai_api_key="fictional-key",
        openai_base_url="https://models.example.test/v1",
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        client = config.create_chat_client(settings, http_client=http_client).bind_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "read",
                        "description": "读取来源片段",
                        "parameters": {
                            "type": "object",
                            "properties": {"segment_id": {"type": "string"}},
                            "required": ["segment_id"],
                        },
                    },
                }
            ]
        )
        messages = [HumanMessage("读取 sample 片段")]
        if stream_tool_turn:
            chunks = list(client.stream(messages))
            combined = chunks[0]
            for chunk in chunks[1:]:
                combined += chunk
            tool_response = message_chunk_to_message(combined)
        else:
            tool_response = client.invoke(messages)
        assert tool_response.additional_kwargs["reasoning_content"] == tool_reasoning
        assert tool_response.tool_calls == [
            {
                "name": "read",
                "args": {"segment_id": "sample"},
                "id": tool_call["id"],
                "type": "tool_call",
            }
        ]
        messages += [
            tool_response,
            ToolMessage("来源正文", name="read", tool_call_id=tool_call["id"]),
        ]
        final = client.invoke(messages)
        assert seen[1]["messages"][-2]["reasoning_content"] == tool_reasoning
        # LangChain 解析后重新序列化工具参数，JSON 空白可以变化；协议推理原文不能变化。
        outgoing_calls = seen[1]["messages"][-2]["tool_calls"]
        assert len(outgoing_calls) == 1
        assert outgoing_calls[0]["id"] == tool_call["id"]
        assert outgoing_calls[0]["type"] == "function"
        assert outgoing_calls[0]["function"]["name"] == "read"
        assert json.loads(outgoing_calls[0]["function"]["arguments"]) == {"segment_id": "sample"}
        assert seen[1]["messages"][-1]["role"] == "tool"
        assert seen[1]["messages"][-1]["tool_call_id"] == tool_call["id"]
        assert seen[1]["messages"][-1]["content"] == "来源正文"
        assert final.additional_kwargs["reasoning_content"] == final_reasoning
        # 下一用户轮仍带 tools：工具轮和无工具的最终回复轮都需要原样回传协议字段。
        followup = client.invoke([*messages, final, HumanMessage("解释已读取的片段")])
        assert len(seen) == 3
        assert all(payload["tools"] for payload in seen)
        assert seen[2]["messages"][1]["reasoning_content"] == tool_reasoning
        assert seen[2]["messages"][-2]["reasoning_content"] == final_reasoning
        assert "reasoning_content" not in followup.additional_kwargs
        client.invoke(
            [*messages, final, HumanMessage("解释已读取的片段"), followup, HumanMessage("继续")]
        )
        assert "reasoning_content" not in seen[3]["messages"][-2]
