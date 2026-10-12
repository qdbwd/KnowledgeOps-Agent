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
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlsplit

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
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    environment_refs: dict[str, str] = Field(default_factory=dict)
    header_refs: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=30, gt=0, le=120)

    @model_validator(mode="after")
    def validate_transport(self):
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
            if self.command or self.args or self.environment_refs:
                raise ValueError("HTTP configuration cannot launch a local process")
        elif not self.command or self.url or self.header_refs:
            raise ValueError("stdio requires an installed command and separate arguments")
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


@asynccontextmanager
async def connected_tools(
    config: MCPConnectionConfig, *, secrets: Mapping[str, str]
) -> AsyncIterator[list[BaseTool]]:
    """仅在上下文内保活连接，退出释放子进程/HTTP客户端；不隐式安装或重试。

    stdio按argv执行现有程序，禁止把配置拼成shell命令。HTTP凭据通过
    header引用注入；错误脱敏、许可、取消和业务回执由上层调用服务处理。
    """
    transport: StdioTransport | StreamableHttpTransport
    if config.transport == "stdio":
        executable = shutil.which(config.command or "")
        if executable is None:
            raise ValueError("configured MCP command is not installed")
        transport = StdioTransport(
            command=executable,
            args=config.args,
            env=child_environment(resolve_secret_refs(config.environment_refs, secrets)),
            keep_alive=False,
        )
    else:
        transport = StreamableHttpTransport(
            url=config.url or "",
            headers=resolve_secret_refs(config.header_refs, secrets),
        )
    client = Client(
        transport,
        name="KnowledgeOps",
        sampling_handler=_deny_sampling,
        timeout=config.timeout_seconds,
        init_timeout=config.timeout_seconds,
        cache=False,
    )
    async with MCPAdapter(client) as adapter:
        yield await adapter.list_tools()
