"""显式读取本项目配置；不扫描个人知识库或任意上级目录。

生产由 Compose、开发由 dev.py 在启动时注入配置，直接调用时可读取项目根 .env。进程环境和
构造参数优先于文件。测试使用 _env_file=None 与虚构凭据。
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from knowledgeops_agent.core.resources import source_project_root

PROJECT_ROOT = source_project_root()


class Settings(BaseSettings):
    """基础设施配置不进入 AgentState；密钥和数据库 DSN 不出现在 repr。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env" if PROJECT_ROOT is not None else None,
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )
    database_url: str = Field(default="", repr=False)
    accounting_database_url: str = Field(default="", repr=False)
    source_storage_root: Path = Path("/data/sources")
    # Compose 显式启用；库级测试/旧部署未设置时保留原存储合同。
    workspace_root: Path | None = None
    workspace_execute_timeout: int = Field(default=60, ge=1, le=300)
    workspace_output_limit_bytes: int = Field(default=10485760, ge=1024, le=104857600)
    celery_broker_url: str = Field(default="redis://redis:6379/0", repr=False)
    # DB 1 仅作可丢弃加速层；DB 0 继续承载 Celery。空 URL 可整体关闭加速。
    runtime_redis_url: str = Field(default="", repr=False)
    redis_events_enabled: bool = True
    redis_checkpoint_cache_enabled: bool = True
    redis_event_max_entries: int = Field(default=1024, ge=1, le=10000)
    redis_event_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    redis_checkpoint_ttl_seconds: int = Field(default=900, ge=60, le=86400)
    redis_checkpoint_max_entries: int = Field(default=128, ge=1, le=1024)
    redis_checkpoint_max_value_bytes: int = Field(default=2097152, ge=1024, le=8388608)
    # 只清理满足业务保护条件的数据；期限不授予删除独立资产的许可。
    lifecycle_cleanup_enabled: bool = True
    lifecycle_retention_days: int = Field(default=30, ge=1, le=3650)
    lifecycle_scan_interval_seconds: int = Field(default=86400, ge=60, le=604800)
    lifecycle_batch_size: int = Field(default=100, ge=1, le=1000)
    lifecycle_temporary_grace_seconds: int = Field(default=86400, ge=60, le=604800)
    lifecycle_job_retention_days: int = Field(default=30, ge=1, le=3650)
    lifecycle_job_lease_seconds: int = Field(default=60, ge=10, le=600)
    lifecycle_retry_seconds: int = Field(default=60, ge=1, le=3600)
    # worker 内 MCP 连接复用。未配置任何 MCP 时不创建连接，也不启动维护线程。
    mcp_session_max_connections: int = Field(default=32, ge=1, le=256)
    mcp_session_idle_seconds: float = Field(default=1800, ge=1, le=86400)
    mcp_session_sweep_seconds: float = Field(default=2, ge=0.1, le=60)
    mcp_session_close_seconds: float = Field(default=5, ge=0.1, le=30)
    model_calls_enabled: bool = False
    openai_api_key: SecretStr = SecretStr("")
    openai_base_url: str = Field(default="", repr=False)
    agent_model: str = "deepseek-flash"
    model_provider: Literal["deepseek", "openai-compatible"] = "deepseek"
    model_timeout_seconds: float = Field(default=45, gt=0, le=180)
    api_allowed_hosts: str = "localhost,127.0.0.1,::1"
    # 与 Uvicorn/Celery 共同支持的级别保持一致，避免仅 API 读取了应用日志配置。
    app_log_level: Literal["critical", "error", "warning", "info", "debug"] = "info"

    @field_validator("api_allowed_hosts")
    @classmethod
    def explicit_hosts(cls, value: str) -> str:
        """部署可增加明确主机名；通配符不能取消同源和 DNS 重绑定防护。"""
        hosts = [host.strip().lower() for host in value.split(",")]
        if not hosts or any(not host or any(char in host for char in "/*?#@") for host in hosts):
            raise ValueError("API_ALLOWED_HOSTS must contain explicit hostnames")
        return ",".join(dict.fromkeys(hosts))

    @model_validator(mode="after")
    def require_online_credentials(self):
        """只在在线模式要求密钥；离线数据库/页面检查无需供应商凭据。"""
        if self.model_calls_enabled:
            if not self.openai_api_key.get_secret_value().strip():
                raise ValueError("OPENAI_API_KEY is required when model calls are enabled")
            url = urlsplit(self.openai_base_url)
            if url.scheme not in {"http", "https"} or not url.hostname:
                raise ValueError("OPENAI_BASE_URL must be an explicit HTTP(S) endpoint")
            if not self.agent_model.strip():
                raise ValueError("AGENT_MODEL must not be empty")
        return self


@lru_cache
def get_settings() -> Settings:
    """进程内共享不可含运行状态的配置；环境变更需要重启服务。"""
    return Settings()


def create_chat_client(settings: Settings, **transport_options):
    """保留供应商协议字段的客户端；依然只允许通过统一网关实际请求。"""
    from langchain_openai import ChatOpenAI

    from knowledgeops_agent.harness.models.deepseek import DeepSeekClient

    client_type = DeepSeekClient if settings.model_provider == "deepseek" else ChatOpenAI
    return client_type(
        model=settings.agent_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        timeout=settings.model_timeout_seconds,
        max_retries=0,
        use_responses_api=False,
        stream_usage=True,
        **transport_options,
    )


def create_gateway(settings: Settings):
    """构造统一网关；创建客户端本身不请求模型，SDK 自动重试明确为零。"""
    from sqlalchemy import create_engine

    from knowledgeops_agent.harness.context.capacity import load_model_capacity
    from knowledgeops_agent.harness.models.gateway import (
        ModelGateway,
        ModelLedger,
        prepare_accounting,
    )

    if not settings.accounting_database_url:
        raise ValueError("ACCOUNTING_DATABASE_URL is required")
    engine = create_engine(settings.accounting_database_url, pool_pre_ping=True)
    prepare_accounting(engine)
    client = None
    if settings.model_calls_enabled:
        client = create_chat_client(settings)
    return ModelGateway(
        ModelLedger(engine),
        enabled=settings.model_calls_enabled,
        client=client,
        sensitive_values=[settings.openai_api_key.get_secret_value()],
        capacity=load_model_capacity(settings.agent_model, settings.openai_base_url)
        if settings.model_calls_enabled
        else None,
    )
