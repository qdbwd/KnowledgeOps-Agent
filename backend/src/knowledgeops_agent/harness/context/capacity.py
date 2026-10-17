"""完整请求容量合同：真实端点容量、应用工作窗口及估计方法分开记录。

使用官方DeepSeek模板及词表离线计数；供应商实际usage另存。旧运行快照
保留原字节估算合同，不能在恢复时悄悄改变计数方法或扩大容量。
"""

import json
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from knowledgeops_agent.core.resources import bundled_resource, resource_path


class ContextCapacityExceeded(RuntimeError):
    """请求尚未发送：需要压缩旧历史、分段材料或缩小必需规则/schema。"""


class ModelCapacity(BaseModel):
    """不可变容量设置；工作窗口是成本选择，不能冒充模型本身的限制。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    model: str = "deepseek-flash"
    endpoint_host: str = "api.deepseek.com"
    context_window: int = Field(default=1048576, gt=0)
    max_output_tokens: int = Field(default=393216, gt=0)
    working_window: int = Field(default=262144, gt=0)
    output_reserve: int = Field(default=8192, gt=0)
    framing_reserve: int = Field(default=4096, ge=0)
    summary_fraction: float = Field(default=0.9, gt=0, lt=1)
    summary_trigger_basis: Literal["input_tokens", "total_tokens"] = "input_tokens"
    compact_after_turn: bool = True
    # 新运行按完整输入分配容量；旧快照恢复沿用原保留策略。
    input_budget_version: int = Field(default=1, ge=0, le=1)
    compact_target_fraction: float = Field(default=0.8, gt=0, lt=1)
    optional_background_tokens: int = Field(default=16384, ge=0)
    optional_background_fraction: float = Field(default=0.125, gt=0, le=1)
    deferred_mcp_tools: bool = True
    token_counter: Literal["deepseek_v41_tokenizer_v1", "utf8_bytes_plus_framing_v1"] = (
        "deepseek_v41_tokenizer_v1"
    )
    source: str = "GET /models; artifacts/verification/optimization/model-profile-discovery.json"
    verified_at: str = "2026-10-09"

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> "ModelCapacity":
        """旧冻结数据缺少计数器版本时，沿用旧方法；不修改原快照和哈希。"""
        return cls.model_validate(
            {
                "token_counter": "utf8_bytes_plus_framing_v1",
                "summary_trigger_basis": "total_tokens",
                "compact_after_turn": False,
                "input_budget_version": 0,
                "deferred_mcp_tools": False,
                **snapshot,
            }
        )

    @model_validator(mode="after")
    def check_bounds(self):
        if self.working_window > self.context_window:
            raise ValueError("working window exceeds verified model capacity")
        if self.output_reserve > self.max_output_tokens:
            raise ValueError("output reserve exceeds provider limit")
        if self.output_reserve + self.framing_reserve >= self.working_window:
            raise ValueError("working window leaves no input capacity")
        if self.input_budget_version and self.compact_target_fraction >= self.summary_fraction:
            raise ValueError("compaction target must be below the trigger fraction")
        return self

    def measure(self, payload: dict[str, Any], output_tokens: int | None = None) -> dict:
        """只计算不修改输入；输入token、输出预留和协议余量分别记录。"""
        reserve = self.output_reserve if output_tokens is None else output_tokens
        content = {
            key: value
            for key, value in payload.items()
            if key not in {"max_tokens", "max_completion_tokens", "stream"}
        }
        method: str
        if self.token_counter == "utf8_bytes_plus_framing_v1":
            serialized = json.dumps(
                content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            estimate = len(serialized.encode("utf-8")) + 32 * len(payload.get("messages", []))
            method = self.token_counter
        else:
            from knowledgeops_agent.harness.context.tokens import count_request_tokens

            estimate, method = count_request_tokens(content)
        return {
            "estimated": True,
            "method": method,
            "input_tokens": estimate,
            "output_tokens": reserve,
            "framing_reserve": self.framing_reserve,
            "total_tokens": estimate + reserve + self.framing_reserve,
            "working_window": self.working_window,
            "model_window": self.context_window,
            "model": self.model,
            "summary_fraction": self.summary_fraction,
            "summary_trigger_basis": self.summary_trigger_basis,
            "summary_trigger_tokens": int(self.working_window * self.summary_fraction),
            "profile_verified_at": self.verified_at,
        }

    def validate_request(self, payload: dict[str, Any], output_tokens: int) -> dict:
        """发出前的最终护栏；失败不截断输入，也不消费模型请求额度。"""
        if not 0 < output_tokens <= self.max_output_tokens:
            raise ContextCapacityExceeded("request capacity: output limit exceeds verified profile")
        evidence = self.measure(payload, output_tokens)
        if evidence["total_tokens"] > self.working_window:
            raise ContextCapacityExceeded(
                "request capacity exceeded: 必需输入与输出预留超出工作窗口，请分段处理材料，"
                "或减少规则/工具说明；原始记录保留，尚未调用模型。"
            )
        return evidence


def load_model_capacity(model: str, base_url: str) -> ModelCapacity:
    """仅对核实过的端点和模型启用配置；同名别名不能自动继承其他渠道容量。"""
    path = resource_path(
        "MODEL_CAPACITY_PATH", bundled_resource("config/model-capacity.json")
    )
    profile = ModelCapacity.model_validate_json(path.read_text(encoding="utf-8"))
    if model != profile.model or urlsplit(base_url).hostname != profile.endpoint_host:
        raise ValueError("verified model capacity profile does not match the configured endpoint")
    return profile
