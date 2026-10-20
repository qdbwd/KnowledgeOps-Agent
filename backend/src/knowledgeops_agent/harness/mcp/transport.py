"""MCP协议薄适配：配置只保存环境变量引用，调用能力不等于业务许可。

使用锁定LangChain 1.4.3的MCPAdapter/FastMCP连接生命周期，不另造协议。
本模块只发现/适配工具，业务层仍需筛选许可、冻结schema、执行前授权并
记录回执。模型不会收到连接配置，也不能凭工具注解自行取得写入权限。
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastmcp.client import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from langchain.mcp import MCPAdapter
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, model_validator


class MCPConnectionConfig(BaseModel):
    """可持久化的非秘密连接配置；密钥仅由受信任应用进程在调用时注入。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    transport: Literal["stdio", "http"]
    command: str | None = None
    installation_id: str | None = None
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    url: str | None = None
    environment_refs: dict[str, str] = Field(default_factory=dict)
    header_refs: dict[str, str] = Field(default_factory=dict)
    header_prefixes: dict[str, Literal["Bearer ", "Basic "]] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    # SDK 4 默认协商新版逐请求协议；依赖持久会话的服务可明确选择握手协议。
    protocol_mode: Literal["auto", "legacy"] = "auto"

    @model_validator(mode="after")
    def validate_transport(self):
        if self.installation_id and (self.transport != "stdio" or str(UUID(self.installation_id)) != self.installation_id):
            raise ValueError("managed installations require a stdio UUID identity")
        if self.transport == "http":
            url = urlsplit(self.url or "")
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("MCP URL must be HTTP(S) without embedded credentials")
            if self.command or self.args or self.environment_refs or self.cwd:
                raise ValueError("HTTP configuration cannot launch a local process")
        elif not self.command or self.url or self.header_refs or self.header_prefixes:
            raise ValueError("stdio requires an installed command and separate arguments")
        if self.cwd and (not PurePosixPath(self.cwd).is_absolute() or "\\" in self.cwd
                         or "\x00" in self.cwd or "${" in self.cwd or ".." in PurePosixPath(self.cwd).parts):
            raise ValueError("MCP cwd must be an absolute container directory")
        if set(self.header_prefixes) - self.header_refs.keys():
            raise ValueError("header prefixes require a corresponding environment reference")
        for ref in [*self.environment_refs.values(), *self.header_refs.values()]:
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", ref):
                raise ValueError("secret references must be environment variable names")
        return self


def resolve_secret_refs(refs: Mapping[str, str], secrets: Mapping[str, str]) -> dict[str, str]:
    """缺少密钥时在连接前失败；错误只报告变量名，不回显凭据内容。"""
    resolved = {}
    for name, reference in refs.items():
        if not secrets.get(reference):
            raise ValueError(f"missing MCP environment reference: {reference}")
        resolved[name] = secrets[reference]
    return resolved


def child_environment(overrides: Mapping[str, str]) -> dict[str, str]:
    """本地MCP子进程仅继承基础运行环境和显式映射，不继承模型/数据库密钥。"""
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "WINDIR"}
    return {**{name: value for name, value in os.environ.items() if name in allowed}, **overrides}


async def _deny_sampling(*args, **kwargs):
    """MCP服务不能通过sampling绕过项目模型网关和费用账本。"""
    raise PermissionError("MCP sampling is disabled; model calls must use the application gateway")


async def _unsupported_live_input(*args, **kwargs):
    """旧式活连接补问不能跨 worker 等待；明确报告限制，不伪造真人拒绝。"""
    raise NotImplementedError(
        "当前客户端不支持旧式依赖活连接的 MCP 补问；请使用支持 InputRequiredResult 的现代协议服务。"
    )


@asynccontextmanager
async def connected_adapter(
    config: MCPConnectionConfig, *, secrets: Mapping[str, str]
) -> AsyncIterator[MCPAdapter]:
    """仅在上下文内保活连接，退出释放子进程/HTTP客户端；不隐式安装或重试。

    stdio按argv执行现有程序，禁止把配置拼成shell命令。HTTP凭据通过
    header引用注入；错误脱敏、许可、取消和业务回执由上层调用服务处理。
    """
    from knowledgeops_agent.harness.mcp.installations import MCPInstallStorage

    storage = MCPInstallStorage()
    if not config.installation_id:
        storage.reject_external_references(config.model_dump())
    # 退出协议上下文并回收子进程之后才释放目录使用锁。
    with storage.use(config.installation_id) if config.installation_id else nullcontext() as manifest:
        if manifest and config.installation_id and str(storage.release(config.installation_id) / manifest['entrypoint']) != config.command:
            raise ValueError("managed MCP command does not match its prepared installation")
        async with _connected_adapter(config, secrets=secrets) as adapter:
            yield adapter


@asynccontextmanager
async def _connected_adapter(config: MCPConnectionConfig, *, secrets: Mapping[str, str]) -> AsyncIterator[MCPAdapter]:
    transport: StdioTransport | StreamableHttpTransport
    if config.transport == "stdio":
        executable = shutil.which(config.command or "")
        if executable is None:
            raise ValueError("configured MCP command is not installed")
        transport = StdioTransport(
            command=executable,
            args=config.args,
            cwd=config.cwd,
            env=child_environment(resolve_secret_refs(config.environment_refs, secrets)),
            keep_alive=False,
            # 第三方服务的stderr可能输出凭据；应用诊断只保留受控错误类别/协议回执。
            log_file=Path(os.devnull),
        )
    else:
        transport = StreamableHttpTransport(
            url=config.url or "",
            headers={name: config.header_prefixes.get(name, "") + value
                     for name, value in resolve_secret_refs(config.header_refs, secrets).items()},
        )
    client = Client(
        transport,
        name="KnowledgeOps",
        sampling_handler=_deny_sampling,
        elicitation_handler=_unsupported_live_input,
        timeout=config.timeout_seconds,
        init_timeout=config.timeout_seconds,
        cache=False,
        mode=config.protocol_mode,
    )
    async with MCPAdapter(client) as adapter:
        yield adapter


@asynccontextmanager
async def connected_tools(
    config: MCPConnectionConfig, *, secrets: Mapping[str, str]
) -> AsyncIterator[list[BaseTool]]:
    """发现目录使用短连接；运行时由会话管理器持有 connected_adapter 的外层上下文。"""
    async with connected_adapter(config, secrets=secrets) as adapter:
        yield await adapter.list_tools()
