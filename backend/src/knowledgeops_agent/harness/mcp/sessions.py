"""复用官方 MCPAdapter 的连接；应用只管理作用域、容量、失效与退出。

参考 DeerFlow 4e9f6c6 的 MCP session_pool 所有者任务、作用域隔离与 LRU 设计。
没有复制其绑定旧适配包的实现。AnyIO BlockingPortal 为同步 LangGraph 工具提供
一个持续事件循环；连接建立和退出留在同一协程，避免跨 AnyIO cancel scope 关闭。
池不保存到检查点，也不自动重放断线时的业务调用。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, AsyncExitStack, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from anyio.from_thread import BlockingPortal, start_blocking_portal

from knowledgeops_agent.harness.mcp.transport import (
    MCPConnectionConfig,
    child_environment,
    connected_adapter,
    resolve_secret_refs,
)

logger = logging.getLogger(__name__)


@dataclass
class SessionLease:
    adapter: Any = field(repr=False)
    session_id: str
    reused: bool
    protocol_version: str | None


@dataclass
class _Entry:
    scope: str
    binding: dict = field(repr=False)
    fingerprint: str = field(repr=False)
    last_used: float
    ready: asyncio.Future = field(repr=False)
    session_id: str = field(default_factory=lambda: str(uuid4()))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    borrowers: set[asyncio.Task] = field(default_factory=set, repr=False)
    owner: asyncio.Task | None = field(default=None, repr=False)
    cleanup: asyncio.Task | None = field(default=None, repr=False)
    uses: int = 0
    retired: bool = False


class MCPSessionManager:
    """每个 worker 持有一池；同作用域/服务串行借用，不同作用域不共用客户端。

    run 是同步边界，lease 在专有事件循环内使用。默认未调用则不启动线程。
    is_valid 从应用权威配置复查撤回；凭据仅进连接，指纹和日志不包含明文。
    """

    def __init__(
        self,
        *,
        connector=connected_adapter,
        is_valid: Callable[[str, dict], bool] | None = None,
        secrets: Mapping[str, str] | None = None,
        max_connections: int = 32,
        idle_seconds: float = 1800,
        sweep_seconds: float = 2,
        close_seconds: float = 5,
        clock: Callable[[], float] = time.monotonic,
    ):
        if max_connections < 1 or min(idle_seconds, sweep_seconds, close_seconds) <= 0:
            raise ValueError("MCP session limits must be positive")
        self.connector, self.is_valid, self.secrets = connector, is_valid, secrets
        self.max_connections, self.idle_seconds = max_connections, idle_seconds
        self.sweep_seconds, self.close_seconds, self.clock = sweep_seconds, close_seconds, clock
        self._thread_lock = threading.RLock()
        self._closed = False
        self._portal: BlockingPortal | None = None
        self._portal_context: AbstractContextManager[BlockingPortal] | None = None
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._retiring: dict[str, _Entry] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _get_portal(self):
        with self._thread_lock:
            if self._closed:
                raise RuntimeError("MCP session manager is closed")
            if self._portal is None:
                self._portal_context = start_blocking_portal(name="knowledgeops-mcp")
                self._portal = self._portal_context.__enter__()
                self._portal.call(self._start)
            return self._portal

    def run(self, function, *args):
        """跨同步工具调用、不同调用者线程和临时事件循环，使用同一个连接所有者循环。"""
        return self._get_portal().call(function, *args)

    async def _start(self):
        self._lock = asyncio.Lock()
        self._stopping = False
        self._sweeper = asyncio.create_task(self._maintain())

    def _identity(self, binding: dict) -> tuple[MCPConnectionConfig, dict[str, str], str]:
        config = MCPConnectionConfig.model_validate(binding["config"])
        secrets = dict(os.environ if self.secrets is None else self.secrets)
        identity = {
            "config": config.model_dump(),
            "epoch": binding["authorization_epoch"],
            "environment": child_environment(resolve_secret_refs(config.environment_refs, secrets))
            if config.transport == "stdio"
            else {},
            "headers": resolve_secret_refs(config.header_refs, secrets),
        }
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return config, secrets, fingerprint

    async def _valid(self, scope: str, binding: dict) -> bool:
        if self.is_valid is None:
            return True
        # 配置存于 PG；同步短查询不能占住协议事件循环，否则心跳/取消也被阻塞。
        try:
            return bool(await asyncio.to_thread(self.is_valid, scope, binding))
        except Exception as error:
            logger.warning("MCP session permission check failed (%s)", type(error).__name__)
            return False

    async def _own(self, entry: _Entry, config: MCPConnectionConfig, secrets: dict):
        stack = AsyncExitStack()
        try:
            async with asyncio.timeout(config.timeout_seconds):
                adapter = await stack.enter_async_context(self.connector(config, secrets=secrets))
            entry.ready.set_result(adapter)
            await entry.stop.wait()
        except BaseException as error:
            if not entry.ready.done():
                entry.ready.set_exception(error)
        finally:
            try:
                async with asyncio.timeout(self.close_seconds):
                    await stack.aclose()
            except BaseException as error:
                # 不记录异常正文：HTTP 客户端异常可能含 URL、请求头或凭据。
                logger.warning("MCP session cleanup failed (%s)", type(error).__name__)

    async def _borrow(self, scope: str, binding: dict, config, secrets, fingerprint) -> _Entry:
        while True:
            victim = None
            async with self._lock:
                if self._stopping:
                    raise RuntimeError("MCP session manager is closed")
                key = (scope, binding["id"])
                entry = self._entries.get(key)
                if entry and entry.fingerprint != fingerprint:
                    victim = entry
                elif (
                    entry is None
                    and len(self._entries) + len(self._retiring) >= self.max_connections
                ):
                    idle = [item for item in self._entries.values() if not item.borrowers]
                    if not idle:
                        raise RuntimeError(
                            "MCP session capacity reached; connections are busy or closing"
                        )
                    victim = min(idle, key=lambda item: item.last_used)
                if victim is not None:
                    self._detach(victim)
                else:
                    if entry is None:
                        ready = asyncio.get_running_loop().create_future()
                        # 开始连接时调用者也可能已取消，仍消费异常避免后台未读取异常告警。
                        ready.add_done_callback(
                            lambda future: future.exception() if not future.cancelled() else None
                        )
                        entry = _Entry(scope, deepcopy(binding), fingerprint, self.clock(), ready)
                        self._entries[key] = entry
                        entry.owner = asyncio.create_task(self._own(entry, config, secrets))
                    borrower = asyncio.current_task()
                    assert borrower is not None
                    entry.borrowers.add(borrower)
                    return entry
            await self._dispose(victim)

    def _detach(self, entry: _Entry):
        """调用者持池锁；先取消可发现性，再异步关闭，避免旧连接复活或锁内等待死锁。"""
        key = (entry.scope, entry.binding["id"])
        if self._entries.get(key) is entry:
            del self._entries[key]
        if entry.retired:
            return
        entry.retired = True
        self._retiring[entry.session_id] = entry

    async def _finish_close(self, entry: _Entry, caller: asyncio.Task | None):
        try:
            for borrower in list(entry.borrowers):
                if borrower is not caller:
                    borrower.cancel()
            entry.stop.set()
            if entry.owner:
                # 连接尚在握手时也必须立即取消；已连接则由所有者正常退出上下文。
                if not entry.ready.done():
                    entry.owner.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(entry.owner), self.close_seconds + 0.5)
                except asyncio.CancelledError:
                    if not entry.owner.cancelled():
                        raise
                except TimeoutError:
                    entry.owner.cancel()
                    await asyncio.gather(entry.owner, return_exceptions=True)
        finally:
            async with self._lock:
                self._retiring.pop(entry.session_id, None)

    async def _dispose(self, entry: _Entry):
        async with self._lock:
            self._detach(entry)
            if entry.cleanup is None:
                entry.cleanup = asyncio.create_task(
                    self._finish_close(entry, asyncio.current_task())
                )
        # 工具取消不能同时取消资源回收；同一条连接仅执行一次关闭。
        await asyncio.shield(entry.cleanup)

    @asynccontextmanager
    async def lease(self, scope: str, binding: dict):
        """失败不重试；正常退出保留连接，异常/超时/取消退出丢弃可能失效的会话。"""
        if not await self._valid(scope, binding):
            raise PermissionError("MCP connection permission has been revoked")
        config, secrets, fingerprint = self._identity(binding)
        entry = await self._borrow(scope, binding, config, secrets, fingerprint)
        acquired = False
        try:
            async with entry.lock:
                acquired = True
                if entry.retired:
                    raise ConnectionError("MCP session has been closed")
                adapter = await asyncio.shield(entry.ready)
                if not await self._valid(scope, binding):
                    raise PermissionError("MCP connection permission has been revoked")
                reused = entry.uses > 0
                if reused:
                    # FastMCP 工具可重入 client；先确认旧连接健康，禁止在已断线时静默重连。
                    if (
                        hasattr(adapter.client, "is_connected")
                        and not adapter.client.is_connected()
                    ):
                        raise ConnectionError("MCP session disconnected")
                    async with asyncio.timeout(config.timeout_seconds):
                        # 新协议不再提供 ping；tools/list 是两代协议共有的无副作用探针。
                        # 直接在已连接 client 上请求，不让 Adapter 重入时新建会话。
                        try:
                            await adapter.client.list_tools(cache_mode="bypass")
                        except Exception as error:
                            raise ConnectionError("MCP session health check failed") from error
                entry.uses += 1
                yield SessionLease(
                    adapter,
                    entry.session_id,
                    reused,
                    getattr(adapter.client, "protocol_version", None),
                )
        except BaseException:
            if acquired:
                await self._dispose(entry)
            raise
        finally:
            async with self._lock:
                entry.borrowers.discard(asyncio.current_task())
                entry.last_used = self.clock()

    async def sweep(self):
        """空闲淘汰不打断使用者；配置撤回/凭据轮换则关闭包括正在使用的旧连接。"""
        async with self._lock:
            entries = list(self._entries.values())
        for entry in entries:
            valid = await self._valid(entry.scope, entry.binding)
            try:
                valid = valid and self._identity(entry.binding)[2] == entry.fingerprint
            except (ValueError, KeyError):
                valid = False
            async with self._lock:
                idle = not entry.borrowers and self.clock() - entry.last_used >= self.idle_seconds
                expired = not valid or idle
                if expired:
                    self._detach(entry)
            if expired:
                await self._dispose(entry)

    async def _maintain(self):
        while True:
            await asyncio.sleep(self.sweep_seconds)
            await self.sweep()

    async def _release(self, *, scope=None, server=None):
        async with self._lock:
            entries = [
                entry
                for entry in [*self._entries.values(), *self._retiring.values()]
                if (scope is None or entry.scope == scope)
                and (server is None or entry.binding["id"] == server)
            ]
            for entry in entries:
                self._detach(entry)
        await asyncio.gather(*(self._dispose(entry) for entry in entries))

    def close_scope(self, scope: str):
        if self._portal is not None and not self._closed:
            self._portal.call(lambda: self._release(scope=scope))

    def close_server(self, server: str):
        if self._portal is not None and not self._closed:
            self._portal.call(lambda: self._release(server=server))

    async def _shutdown(self):
        self._stopping = True
        self._sweeper.cancel()
        await asyncio.gather(self._sweeper, return_exceptions=True)
        await self._release()

    def close(self):
        """服务退出先关 MCP，再销毁 PG；不用时不创建线程，重复关闭无副作用。"""
        with self._thread_lock:
            if self._closed:
                return
            self._closed = True
            portal, context = self._portal, self._portal_context
        if portal is not None:
            try:
                portal.call(self._shutdown)
            finally:
                assert context is not None
                context.__exit__(None, None, None)
