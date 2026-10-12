"""可校验、可冻结的DDGS搜索设置；不加载凭据，也不修改工具开关和权限。"""

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from knowledgeops_agent.config_resources import bundled_resource, resource_path
from knowledgeops_agent.vendor.deerflow.ddg_search_options import (
    _normalize_backend,
    _resolve_ddgs_region,
)

SEARCH_CONFIG_PATH = bundled_resource("config/web-search.yaml")
SearchTimeRange = Literal["day", "week", "month", "year"]
DDGS_TIMELIMIT = {"day": "d", "week": "w", "month": "m", "year": "y"}
# 与uv.lock中DDGS 9.16.0实际引擎源码核对。升级库时一起复查，不能根据名字猜能力。
TEXT_BACKENDS = {
    "brave",
    "duckduckgo",
    "google",
    "grokipedia",
    "mojeek",
    "startpage",
    "wikipedia",
    "yahoo",
}
TIME_RANGE_BACKENDS = ("brave", "duckduckgo", "google", "startpage", "yahoo")


class WebSearchConfig(BaseModel):
    """配置控制搜索方式；工具参数只覆盖本次条数/时间范围，不可改执行权限。

    max_results是省略count时的默认值，现有每次最多8条的边界保持。
    region=auto保留本项目中文cn-zh、其他wt-wt的默认策略；含Wikipedia时再
    使用DeerFlow地区修正。时间筛选是搜索引擎的提示，不能证明网页日期真实。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    version: Literal[1] = 1
    backend: str = "brave,duckduckgo,startpage,yahoo"
    region: str = "auto"
    safesearch: Literal["on", "moderate", "off"] = "moderate"
    max_results: int = Field(default=5, ge=1, le=8, strict=True)
    timeout_seconds: int = Field(default=15, ge=1, le=60, strict=True)

    @field_validator("backend", mode="before")
    @classmethod
    def valid_backend(cls, value: Any) -> str:
        """允许YAML列表或逗号字符串；拒绝拼错引擎，避免DDGS静默退回auto。"""
        if not isinstance(value, (str, list, tuple)) or not value:
            raise ValueError("backend must be a nonempty name or list")
        entries = [value] if isinstance(value, str) else value
        if any(not isinstance(item, str) or not item.strip() for item in entries):
            raise ValueError("backend entries must be nonempty strings")
        parts = [part.strip().lower() for part in _normalize_backend(value).split(",")]
        if any(part not in TEXT_BACKENDS | {"auto", "all"} for part in parts):
            raise ValueError("backend contains an unsupported DDGS 9.16 text engine")
        if len(parts) > 1 and {"auto", "all"} & set(parts):
            raise ValueError("auto/all cannot be combined with explicit backends")
        return ",".join(dict.fromkeys(parts))

    @field_validator("region")
    @classmethod
    def valid_region(cls, value: str) -> str:
        value = value.strip().lower()
        if value != "auto" and not re.fullmatch(r"[a-z]{2,3}-[a-z]{2,3}", value):
            raise ValueError("region must be auto or a DDGS locale such as cn-zh/us-en/wt-wt")
        return value

    @classmethod
    def from_snapshot(cls, value: dict | None) -> "WebSearchConfig":
        """老快照沿用当时写死的配置，不回写历史，也不读取今天的YAML。"""
        if value is not None:
            return cls.model_validate(value)
        return cls(
            backend="brave,duckduckgo,startpage,yahoo",
            region="auto",
            safesearch="moderate",
            max_results=5,
            timeout_seconds=15,
        )

    def search_kwargs(
        self, query: str, *, count: int | None = None, time_range: SearchTimeRange | None = None
    ) -> dict[str, Any]:
        """解析一次调用的有效参数，不执行网络；不支持时间筛选时明确反馈。"""
        count = self.max_results if count is None else count
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 8:
            raise ValueError("count must be an integer from 1 to 8")
        backend = self.backend
        if time_range is not None:
            if time_range not in DDGS_TIMELIMIT:
                raise ValueError("time_range must be day, week, month or year")
            # 采用DeerFlow按能力筛选的办法，但显式引擎集合不悄悄扩大到其他服务。
            selected = backend.split(",")
            capable = (
                list(TIME_RANGE_BACKENDS)
                if backend in {"auto", "all"}
                else [name for name in selected if name in TIME_RANGE_BACKENDS]
            )
            if not capable:
                raise ValueError(
                    "configured backends do not support time_range; omit it or change configuration"
                )
            backend = ",".join(capable)
        region = self.region
        if region == "auto":
            region = "cn-zh" if any("\u4e00" <= letter <= "\u9fff" for letter in query) else "wt-wt"
        kwargs: dict[str, Any] = {
            "backend": backend,
            "region": _resolve_ddgs_region(query, region, backend),
            "safesearch": self.safesearch,
            "max_results": count,
        }
        if time_range is not None:
            kwargs["timelimit"] = DDGS_TIMELIMIT[time_range]
        return kwargs


def load_web_search_config(path: Path | None = None) -> WebSearchConfig:
    """新根任务冻结前读取；文件损坏/未知字段明确失败，不静默替换用户配置。"""
    path = path or resource_path("WEB_SEARCH_CONFIG_PATH", SEARCH_CONFIG_PATH)
    try:
        return WebSearchConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid web_search configuration: {exc}") from exc
