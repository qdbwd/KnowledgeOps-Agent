"""验证真实图和工具的消息往返及边界；所有决策来源都是本地测试替身。"""

import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langsmith import tracing_context

from agent_loop.cli import main
from agent_loop.models import DEFAULT_QUESTION, ScriptedModel, create_online_model
from agent_loop.runtime import build_agent, initial_state


@pytest.fixture(autouse=True)
def no_remote_tracing():
    """测试不继承机器上的远程追踪开关，也不需要任何模型凭据。"""
    with tracing_context(enabled=False):
        yield


def run_script(scenario="success", **budgets):
    """通过生产图运行离线情节，避免测试只验证与实现重复的假循环。"""
    return build_agent(ScriptedModel(scenario), **budgets).invoke(initial_state(DEFAULT_QUESTION))


def assert_paired_messages(messages):
    """每批工具请求必须收到且只收到一份同 ID 结果，才能进入下次模型输出。"""
    pending = set()
    for message in messages:
        if isinstance(message, AIMessage):
            assert not pending, "模型在尚未收到全部工具结果时继续运行"
            pending = {call["id"] for call in message.tool_calls}
        elif isinstance(message, ToolMessage):
            assert message.tool_call_id in pending
            pending.remove(message.tool_call_id)
    assert not pending


def test_success_reads_context_before_answering():
    """完整路径必须真实执行两次不同粒度的读取，并把依据送入答案。"""
    result = run_script()
    assert result["status"] == "answered"
    assert result["model_calls"] == 4
    assert result["tool_calls"] == 5
    readings = [
        json.loads(m.content)
        for m in result["messages"]
        if isinstance(m, ToolMessage) and m.name == "read_document"
    ]
    assert [(r["doc_id"], r["view"]) for r in readings] == [
        ("A", "excerpt"),
        ("B", "excerpt"),
        ("A", "context"),
        ("B", "context"),
    ]
    assert readings[-1]["text"] in result["answer"]
    assert "[A]" in result["answer"] and "[B]" in result["answer"]
    assert_paired_messages(result["messages"])


def test_bad_arguments_are_feedback_and_recovery_continues():
    """参数错误不能伪装成成功结果；后续模型必须能收到它并继续。"""
    result = run_script("recover")
    errors = [m for m in result["messages"] if isinstance(m, ToolMessage) and m.status == "error"]
    assert len(errors) == 1 and "invalid_arguments" in errors[0].content
    assert result["status"] == "answered"
    assert result["model_calls"] == 5 and result["tool_calls"] == 6
    assert_paired_messages(result["messages"])


def test_repeated_actions_stop_at_model_budget():
    """脚本一直要求继续时，程序必须自行限制调用并如实报告未完成。"""
    result = run_script("loop", max_model_calls=2)
    assert result["status"] == "budget_exhausted"
    assert result["model_calls"] == result["tool_calls"] == 2
    assert not result["answer"]
    assert_paired_messages(result["messages"])


def test_tool_budget_mid_batch_keeps_protocol_complete():
    """同批第二个工具被预算阻止时仍需返回错误，不能留下悬空调用。"""
    result = run_script(max_tool_calls=2)
    assert result["status"] == "budget_exhausted"
    assert result["tool_calls"] == 2
    last = result["messages"][-1]
    assert isinstance(last, ToolMessage)
    assert last.status == "error" and "未执行" in last.content
    assert_paired_messages(result["messages"])


def test_unknown_tool_is_returned_to_model():
    """未知工具名成为可观察错误；Runtime 不猜测或执行任意函数。"""
    model = Mock()
    model.invoke.side_effect = [
        AIMessage(content="", tool_calls=[{"name": "missing", "args": {}, "id": "one"}]),
        AIMessage(content="工具不可用，当前无法完成。"),
    ]
    result = build_agent(model).invoke(initial_state("test"))
    observed = model.invoke.call_args_list[1].args[0][-1]
    assert isinstance(observed, ToolMessage) and "unknown_tool" in observed.content
    # answered 的语义只是有最终文本；不把拒答冒充为业务任务验收成功。
    assert result["status"] == "answered"
    assert_paired_messages(result["messages"])


@pytest.mark.parametrize(
    "reply",
    [
        AIMessage(content=""),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "list_documents", "args": {}, "id": "same"},
                {"name": "list_documents", "args": {}, "id": "same"},
            ],
        ),
        AIMessage(
            content="",
            invalid_tool_calls=[
                {"name": "list_documents", "args": "{", "id": "broken", "error": "bad json"},
            ],
        ),
    ],
)
def test_invalid_protocol_stops_before_tools(reply):
    """空回答、重复 ID 和无法解析的调用都不能被当作可执行决策。"""
    model = Mock()
    model.invoke.return_value = reply
    result = build_agent(model).invoke(initial_state("test"))
    assert result["status"] == "protocol_error" and result["tool_calls"] == 0


def test_model_error_does_not_publish_exception_payload():
    """异常类型可观察，但可能含密钥的异常正文不进入结果。"""
    model = Mock()
    model.invoke.side_effect = RuntimeError("sensitive-request-token")
    result = build_agent(model).invoke(initial_state("test"))
    assert result["status"] == "model_error" and result["model_calls"] == 1
    assert result["detail"] == "RuntimeError"
    assert "sensitive-request-token" not in str(result)


def test_runs_do_not_share_history():
    """同一个已编译图可接受两次独立输入，不把上一轮观察带入新任务。"""
    graph = build_agent(ScriptedModel())
    first = graph.invoke(initial_state(DEFAULT_QUESTION))
    second = graph.invoke(initial_state(DEFAULT_QUESTION))
    assert first["model_calls"] == second["model_calls"] == 4
    assert len(first["messages"]) == len(second["messages"])


def test_cli_trace_and_exit_status(tmp_path, capsys):
    """CLI 落盘的是外显运行事实，预算停止使用非零退出码。"""
    path = tmp_path / "trace.json"
    assert main(["--trace", str(path)]) == 0
    artifact = json.loads(path.read_text(encoding="utf-8"))
    assert artifact["mode"] == "scripted" and artifact["status"] == "answered"
    assert artifact["messages"][-1]["type"] == "ai"
    with pytest.raises(SystemExit) as error:
        main(["--trace", str(path)])
    assert error.value.code == 2
    assert main(["--scenario", "loop", "--max-model-calls", "1"]) == 1
    assert "budget_exhausted" in capsys.readouterr().out


def test_online_configuration_binds_real_tools_without_network(monkeypatch):
    """用 mock 检查在线适配的配置和工具绑定，不宣称已验证服务商接口。"""
    provider = pytest.importorskip("langchain_openai")
    constructor = Mock()
    monkeypatch.setattr(provider, "ChatOpenAI", constructor)
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    monkeypatch.setenv("AGENT_MODEL", "configured-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:12345/v1")
    create_online_model()
    assert constructor.call_args.kwargs["max_retries"] == 0
    assert constructor.call_args.kwargs["model"] == "configured-model"
    bound = constructor.return_value.bind_tools.call_args.args[0]
    assert {tool.name for tool in bound} == {"list_documents", "read_document"}


def test_invalid_configuration_fails_before_execution(monkeypatch):
    """空任务、无预算、缺少在线凭据等问题在执行之前反馈。"""
    with pytest.raises(ValueError):
        initial_state(" ")
    with pytest.raises(ValueError):
        build_agent(ScriptedModel(), max_model_calls=0)
    with pytest.raises(SystemExit) as error:
        main(["--question", "another task"])
    assert error.value.code == 2
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        create_online_model()
