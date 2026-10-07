"""读取在线模型配置；只在显式选择 online 模式后使用。

.env 是配置文本，不是自动生效的环境变量。这里用 python-dotenv 解析文件，
再把需要的字段传给客户端；不把整个文件注入 os.environ，避免改变其他组件。
"""

import os
from pathlib import Path

from dotenv import dotenv_values

# 本练习以源码目录运行，uv 默认采用 editable 安装。相对源码定位两个明确
# 的配置文件，即使从 IDE 或别的工作目录启动，也不会误读当前目录的 .env。
EXERCISE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = EXERCISE_ROOT.parents[1]
ENV_FILES = (PROJECT_ROOT / ".env", EXERCISE_ROOT / ".env")

# 同时兼容用户现有的短名称与此前公开的 OPENAI_* 名称。
# 同一来源同时填写两种名称时，以 OPENAI_* 为准；不同来源按下方优先级覆盖。
SETTING_NAMES = {
    "OPENAI_API_KEY": ("OPENAI_API_KEY", "API_KEY"),
    "OPENAI_BASE_URL": ("OPENAI_BASE_URL", "BASE_URL"),
    "AGENT_MODEL": ("AGENT_MODEL",),
}


def load_model_settings() -> dict[str, str]:
    """每次构造在线客户端时读取配置，不缓存密钥、不产生网络请求。

    优先级从低到高：项目根 .env、练习 .env、进程环境变量。
    只合并模型所需的三个字段，忽略主项目开关及其他不相关配置。
    显式空值也会覆盖下层值；必填项是否为空由客户端构造函数检查。
    """
    sources = []
    for path in ENV_FILES:
        if path.is_file():
            try:
                # utf-8-sig 兼容 Windows 编辑器保存的 UTF-8 BOM。
                # 禁用变量插值，使密钥中的特殊字符按原文读取。
                sources.append(dotenv_values(path, encoding="utf-8-sig", interpolate=False))
            except (OSError, UnicodeError) as exc:
                # 不把文件正文、密钥或可能包含敏感信息的底层异常输出给 CLI。
                raise ValueError("无法读取模型 .env 配置，请检查文件权限及 UTF-8 编码") from exc
    sources.append(os.environ)

    settings: dict[str, str] = {}
    for source in sources:
        for canonical_name, aliases in SETTING_NAMES.items():
            for alias in aliases:
                if alias in source:
                    settings[canonical_name] = (source[alias] or "").strip()
                    break
    return settings
