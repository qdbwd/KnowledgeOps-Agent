"""通过在线 CLI 的 mock 客户端验证配置进入真实图，绝不访问模型服务。"""

import os
from unittest.mock import Mock

import pytest

from agent_loop import config
from agent_loop.cli import main
from agent_loop.models import ScriptedModel, create_online_model


def test_online_cli_loads_files_from_other_working_directory(tmp_path, monkeypatch, capsys):
    """根文件提供凭据，练习文件覆盖模型；在线 CLI 确实把配置送入客户端。"""
    provider = pytest.importorskip("langchain_openai")
    project_env = tmp_path / "project.env"
    exercise_env = tmp_path / "exercise.env"
    project_env.write_text(
        'API_KEY="fake-key#suffix"\nBASE_URL=http://localhost:12345/v1\n'
        "AGENT_MODEL=old-model\nMODEL_CALLS_ENABLED=false\n",
        encoding="utf-8-sig",
    )
    exercise_env.write_text("AGENT_MODEL=deepseek-flash\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILES", (project_env, exercise_env))
    other_directory = tmp_path / "other"
    other_directory.mkdir()
    # 工作目录里的同名文件应被忽略，避免 IDE 启动位置改变实际使用的账号。
    (other_directory / ".env").write_text("API_KEY=wrong-key\n", encoding="utf-8")
    monkeypatch.chdir(other_directory)
    before = dict(os.environ)
    constructor = Mock()
    constructor.return_value.bind_tools.return_value = ScriptedModel()
    monkeypatch.setattr(provider, "ChatOpenAI", constructor)

    assert main(["--mode", "online"]) == 0
    assert constructor.call_args.kwargs["api_key"] == "fake-key#suffix"
    assert constructor.call_args.kwargs["base_url"] == "http://localhost:12345/v1"
    assert constructor.call_args.kwargs["model"] == "deepseek-flash"
    assert dict(os.environ) == before
    output = capsys.readouterr().out
    assert "mode=online" in output and "status=answered" in output
    assert "fake-key#suffix" not in output


def test_process_overrides_file_even_with_different_aliases(tmp_path, monkeypatch):
    """进程短名称应能覆盖文件标准名称，避免别名选择破坏来源优先级。"""
    provider = pytest.importorskip("langchain_openai")
    path = tmp_path / ".env"
    path.write_text(
        "OPENAI_API_KEY=file-key\nOPENAI_BASE_URL=http://localhost:1/v1\nAGENT_MODEL=file-model\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ENV_FILES", (path,))
    monkeypatch.setenv("API_KEY", "process-key")
    monkeypatch.setenv("BASE_URL", "http://localhost:2/v1")
    monkeypatch.setenv("AGENT_MODEL", "process-model")
    constructor = Mock()
    monkeypatch.setattr(provider, "ChatOpenAI", constructor)

    create_online_model()
    assert constructor.call_args.kwargs["api_key"] == "process-key"
    assert constructor.call_args.kwargs["base_url"] == "http://localhost:2/v1"
    assert constructor.call_args.kwargs["model"] == "process-model"


def test_scripted_mode_does_not_load_configuration(monkeypatch, capsys):
    """默认离线运行不读取文件，也不因本机已配置密钥而自动联网。"""
    loader = Mock(side_effect=AssertionError("scripted mode must not read model settings"))
    monkeypatch.setattr("agent_loop.models.load_model_settings", loader)
    assert main([]) == 0
    loader.assert_not_called()
    assert "mode=scripted" in capsys.readouterr().out
