"""隔离测试配置，防止开发者本机 .env 影响测试或进入失败输出。"""

import pytest

from agent_loop import config


@pytest.fixture(autouse=True)
def isolated_model_settings(monkeypatch):
    """清除相关进程配置；需要文件的测试必须显式指定自己的临时文件。"""
    monkeypatch.setattr(config, "ENV_FILES", ())
    for aliases in config.SETTING_NAMES.values():
        for name in aliases:
            monkeypatch.delenv(name, raising=False)
