"""供应商能力发现、部署回退和每次运行固定的思考参数。

能力合同参考 DeerFlow 3343522 的 ReasoningEffortCapabilities；本模块适配现有
Chat Completions 网关，不引入第二个 Agent runtime。GET /models 只读取元数据，
不通过试发生成请求猜测档位，不根据模型名字虚构能力。
"""

from __future__ import annotations

import json
from threading import Lock
from time import monotonic
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from knowledgeops_agent.core.resources import bundled_resource, resource_path


class ReasoningProfile(BaseModel):
    """部署者声明接口方言；供应商元数据只能更新档位，不能改变请求通道。"""

    model_config = ConfigDict(extra="forbid")
    provider: Literal["deepseek", "openai-compatible"]
    model: str = Field(min_length=1)
    endpoint_host: str = Field(min_length=1)
    levels: list[str] = Field(default_factory=list, max_length=20)
    default_level: str | None = None
    can_disable: bool = False

    @model_validator(mode="after")
    def valid_levels(self):
        import re

        if len(set(self.levels)) != len(self.levels) or any(
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", level) or level in {"default", "off"}
            for level in self.levels
        ):
            raise ValueError("思考档位必须是唯一的供应商参数值")
        if self.default_level is not None and self.default_level not in self.levels:
            raise ValueError("默认思考档位不在支持列表中")
        if self.provider != "deepseek" and self.can_disable and "none" not in self.levels:
            raise ValueError("OpenAI 兼容接口只有明确支持 none 才能关闭思考")
        return self


def configured_profile(settings) -> ReasoningProfile:
    """只匹配明确的模型、供应商与端点；未知模型保持供应商默认，不猜测档位。"""
    identity = {
        "provider": settings.model_provider,
        "model": settings.agent_model,
        "endpoint_host": urlsplit(settings.openai_base_url).hostname or "",
    }
    path = resource_path("MODEL_REASONING_PATH", bundled_resource("config/model-reasoning.json"))
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("model-reasoning.json 配置版本无效")
    rows = [ReasoningProfile.model_validate(row) for row in value["models"]]
    matched = [
        row for row in rows if all(getattr(row, key) == val for key, val in identity.items())
    ]
    if len(matched) > 1:
        raise ValueError("重复的模型思考配置")
    return (
        matched[0]
        if matched
        else ReasoningProfile(
            **{**identity, "endpoint_host": identity["endpoint_host"] or "unconfigured"}
        )
    )


def reasoning_parameters(profile: ReasoningProfile, selection: str) -> tuple[str, dict]:
    """把合法选择转换为真实请求参数；关闭开关与思考强度是两个不同概念。"""
    level = profile.default_level if selection == "default" else selection
    if level is None:
        return "provider-default", {}
    if level == "off":
        if not profile.can_disable:
            raise ValueError("当前模型未声明支持关闭思考")
        return "off", (
            {"extra_body": {"thinking": {"type": "disabled"}}}
            if profile.provider == "deepseek"
            else {"reasoning_effort": "none"}
        )
    if level not in profile.levels:
        raise ValueError("当前模型不支持该思考档位，请刷新选项后重试")
    parameters: dict = {"reasoning_effort": level}
    if profile.provider == "deepseek":
        parameters["extra_body"] = {"thinking": {"type": "enabled"}}
    return level, parameters


class ReasoningService:
    """进程内短期缓存元数据；错误不泄露端点凭据，回退来源明确可见。"""

    def __init__(self, settings, *, transport=None, ttl_seconds: float = 300):
        self.settings = settings
        self.transport = transport
        self.ttl_seconds = ttl_seconds
        self._cached: dict | None = None
        self._expires = 0.0
        self._lock = Lock()

    def catalog(self) -> dict:
        with self._lock:
            if self._cached is not None and monotonic() < self._expires:
                return dict(self._cached)
            profile = configured_profile(self.settings)
            source = "configuration" if profile.levels else "unavailable"
            note = ""
            # 目前只有 DeepSeek 的此元数据合同经过核对。其他供应商读显式配置，
            # 不能把 OpenAI 兼容的 /models 列表等同于完整能力发现协议。
            if self.settings.model_provider == "deepseek" and self.settings.model_calls_enabled:
                try:
                    base = self.settings.openai_base_url.rstrip("/")
                    parsed = urlsplit(base)
                    if (
                        parsed.scheme not in {"http", "https"}
                        or not parsed.hostname
                        or parsed.username
                        or parsed.password
                        or parsed.query
                        or parsed.fragment
                    ):
                        raise ValueError("invalid endpoint")
                    with httpx.Client(
                        timeout=5, follow_redirects=False, transport=self.transport
                    ) as client:
                        response = client.get(
                            base + "/models",
                            headers={
                                "Authorization": "Bearer "
                                + self.settings.openai_api_key.get_secret_value()
                            },
                        )
                        response.raise_for_status()
                        rows = response.json()["data"]
                    row = next(item for item in rows if item["id"] == profile.model)
                    effort = row["effort"]
                    discovered = ReasoningProfile.model_validate(
                        {
                            **profile.model_dump(),
                            "levels": effort["supported_levels"],
                            "default_level": effort.get("default_level"),
                            "can_disable": True,
                        }
                    )
                    if not discovered.levels:
                        raise ValueError("empty effort metadata")
                    profile = discovered
                    source = "provider"
                except (httpx.HTTPError, ValueError, KeyError, TypeError, StopIteration):
                    note = "供应商能力查询暂不可用，使用已声明的配置；没有声明的档位不展示。"
            self._cached = {**profile.model_dump(), "source": source, "note": note}
            self._expires = monotonic() + self.ttl_seconds
            return dict(self._cached)

    def freeze(self, selection: str = "default") -> dict:
        """请求入口解析一次，后续执行/恢复只读取结果，不再次询问供应商默认值。"""
        catalog = self.catalog()
        return self._freeze(catalog, selection)

    def freeze_local(self, selection: str = "default") -> dict:
        """数据库事务中的自动任务只读当前声明，绝不持锁访问供应商。"""
        return self._freeze(
            {**configured_profile(self.settings).model_dump(), "source": "configuration"}, selection
        )

    @staticmethod
    def _freeze(catalog: dict, selection: str) -> dict:
        profile = ReasoningProfile.model_validate(
            {key: value for key, value in catalog.items() if key in ReasoningProfile.model_fields}
        )
        effective, parameters = reasoning_parameters(profile, selection)
        return {
            "requested": selection,
            "effective": effective,
            "provider": profile.provider,
            "model": profile.model,
            "endpoint_host": profile.endpoint_host,
            "source": catalog["source"],
            "parameters": parameters,
        }


def run_reasoning_parameters(context) -> dict:
    """主/子 Agent 读取同一根快照；旧任务没有此字段时维持原请求行为。"""
    return dict(
        (context.resources or {}).get("model", {}).get("reasoning", {}).get("parameters", {})
    )
