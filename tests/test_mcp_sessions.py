"""会话池生命周期与隔离合同；不使用模型，不访问公共 MCP。"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import pytest

from knowledgeops_agent.harness.mcp.sessions import MCPSessionManager


def binding(identifier="server", revision=1):
    return {
        "id": identifier,
        "revision": revision,
        "authorization_epoch": revision,
        "config": {"transport": "http", "url": "http://localhost/mcp", "timeout_seconds": 1},
    }


class Connections:
    def __init__(self):
        self.opened = []
        self.closed = []

    @asynccontextmanager
    async def connect(self, config, *, secrets):
        adapter = Adapter()
        self.opened.append(adapter)
        owner = asyncio.current_task()
        try:
            yield adapter
        finally:
            assert owner is asyncio.current_task(), "连接的建立和退出应由同一所有者负责"
            self.closed.append(adapter)


class Adapter:
    def __init__(self):
        self.client = self
        self.alive = True

    async def list_tools(self, **kwargs):
        if not self.alive:
            raise ConnectionError("lost")
        return True


async def identify(manager, scope, config):
    async with manager.lease(scope, config) as lease:
        return lease.session_id, lease.reused, lease.adapter


def test_sync_calls_and_fresh_caller_loops_reuse_only_matching_scope():
    connections = Connections()
    with MCPSessionManager(connector=connections.connect) as manager:
        first = manager.run(identify, manager, "chat-a:main", binding())
        second = asyncio.run(
            asyncio.to_thread(manager.run, identify, manager, "chat-a:main", binding())
        )
        other = manager.run(identify, manager, "chat-b:main", binding())
        child = manager.run(identify, manager, "chat-a:child-1", binding())
        assert first[0] == second[0] and not first[1] and second[1]
        assert len({first[0], other[0], child[0]}) == 3
    assert len(connections.opened) == len(connections.closed) == 3


def test_configuration_change_and_explicit_release_do_not_reuse_old_session():
    connections = Connections()
    with MCPSessionManager(connector=connections.connect) as manager:
        first = manager.run(identify, manager, "chat", binding())
        second = manager.run(identify, manager, "chat", binding(revision=2))
        assert first[0] != second[0] and connections.closed == [first[2]]
        manager.close_scope("chat")
        third = manager.run(identify, manager, "chat", binding(revision=2))
        assert third[0] != second[0]
        manager.close_server("server")
        assert len(connections.closed) == 3


def test_idle_reaping_and_lru_capacity_release_resources():
    connections = Connections()
    now = [1.0]
    with MCPSessionManager(
        connector=connections.connect, max_connections=2, idle_seconds=10, clock=lambda: now[0]
    ) as manager:
        first = manager.run(identify, manager, "a", binding())
        now[0] = 2
        manager.run(identify, manager, "b", binding())
        now[0] = 3
        manager.run(identify, manager, "c", binding())
        assert connections.closed == [first[2]]
        now[0] = 20
        manager.run(manager.sweep)
        assert len(connections.closed) == 3


def test_broken_session_is_reported_not_silently_retried():
    connections = Connections()
    with MCPSessionManager(connector=connections.connect) as manager:
        first = manager.run(identify, manager, "chat", binding())
        first[2].alive = False
        with pytest.raises(ConnectionError):
            manager.run(identify, manager, "chat", binding())
        assert len(connections.opened) == 1
        second = manager.run(identify, manager, "chat", binding())
        assert second[0] != first[0]


def test_revocation_sweeps_idle_connections_without_waiting_for_next_call():
    connections = Connections()
    permitted = [True]
    with MCPSessionManager(
        connector=connections.connect, is_valid=lambda _scope, _binding: permitted[0]
    ) as manager:
        manager.run(identify, manager, "chat", binding())
        permitted[0] = False
        manager.run(manager.sweep)
        assert len(connections.closed) == 1
        with pytest.raises(PermissionError):
            manager.run(identify, manager, "chat", binding())


def test_shutdown_is_final_and_does_not_start_an_unused_pool():
    manager = MCPSessionManager()
    manager.close()
    with pytest.raises(RuntimeError, match="closed"):
        manager.run(identify, manager, "chat", binding())


def test_concurrent_borrowers_join_one_session_and_do_not_evict_active_connection():
    connections = Connections()
    with MCPSessionManager(connector=connections.connect, max_connections=1) as manager:

        async def exercise():
            entered, release = asyncio.Event(), asyncio.Event()

            async def hold():
                async with manager.lease("chat", binding()) as lease:
                    entered.set()
                    await release.wait()
                    return lease.session_id

            first = asyncio.create_task(hold())
            await entered.wait()
            waiting = asyncio.create_task(identify(manager, "chat", binding()))
            await asyncio.sleep(0)
            with pytest.raises(RuntimeError, match="capacity"):
                await identify(manager, "other", binding())
            assert not waiting.done() and len(connections.opened) == 1
            release.set()
            initial, following = await asyncio.gather(first, waiting)
            assert initial == following[0] and following[1]

        manager.run(exercise)


def test_cancelling_waiter_preserves_active_call_but_cancelling_owner_discards_session():
    connections = Connections()
    with MCPSessionManager(connector=connections.connect) as manager:

        async def exercise():
            entered, release = asyncio.Event(), asyncio.Event()

            async def hold():
                async with manager.lease("chat", binding()):
                    entered.set()
                    await release.wait()

            active = asyncio.create_task(hold())
            await entered.wait()
            waiting = asyncio.create_task(identify(manager, "chat", binding()))
            await asyncio.sleep(0)
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            assert not connections.closed and not active.done()
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
            assert len(connections.closed) == 1
            await identify(manager, "chat", binding())
            assert len(connections.opened) == 2

        manager.run(exercise)


def test_revocation_cancels_in_flight_call_without_replaying_it():
    connections = Connections()
    permitted = [True]
    with MCPSessionManager(
        connector=connections.connect, is_valid=lambda _scope, _binding: permitted[0]
    ) as manager:

        async def exercise():
            entered = asyncio.Event()

            async def hold():
                async with manager.lease("chat", binding()):
                    entered.set()
                    await asyncio.Event().wait()

            active = asyncio.create_task(hold())
            await entered.wait()
            permitted[0] = False
            await manager.sweep()
            await asyncio.gather(active, return_exceptions=True)
            assert active.cancelled()
            assert len(connections.opened) == len(connections.closed) == 1

        manager.run(exercise)


def test_shutdown_during_connection_handshake_finishes_and_releases_owner():
    started, stopped = threading.Event(), threading.Event()

    @asynccontextmanager
    async def slow_connect(config, *, secrets):
        try:
            started.set()
            await asyncio.Event().wait()
            yield Adapter()
        finally:
            stopped.set()

    manager = MCPSessionManager(connector=slow_connect, close_seconds=0.2)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(manager.run, identify, manager, "chat", binding())
        assert started.wait(timeout=5)
        manager.close()
        assert stopped.is_set()
        assert result.done()


def test_credentials_rotation_replaces_connection_without_exposing_values():
    connections = Connections()
    secrets = {"MCP_TEST_TOKEN": "fictional-token-one"}
    config = binding()
    config["config"]["header_refs"] = {"Authorization": "MCP_TEST_TOKEN"}
    with MCPSessionManager(connector=connections.connect, secrets=secrets) as manager:
        first = manager.run(identify, manager, "chat", config)
        secrets["MCP_TEST_TOKEN"] = "fictional-token-two"
        second = manager.run(identify, manager, "chat", config)
        assert first[0] != second[0] and connections.closed == [first[2]]
        assert "fictional-token" not in repr(manager._entries)


def test_closing_connection_still_counts_towards_capacity():
    @asynccontextmanager
    async def slow_close(config, *, secrets):
        try:
            yield Adapter()
        finally:
            closing.set()
            await release.wait()

    with MCPSessionManager(connector=slow_close, max_connections=1) as manager:
        async def exercise():
            nonlocal closing, release
            closing, release = asyncio.Event(), asyncio.Event()
            await identify(manager, "first", binding())
            replacement = asyncio.create_task(identify(manager, "second", binding()))
            await closing.wait()
            with pytest.raises(RuntimeError, match="capacity"):
                await identify(manager, "third", binding())
            release.set()
            await replacement

        closing = release = None
        manager.run(exercise)
