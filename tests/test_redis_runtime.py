"""Redis 只提供加速；真实 PG 事务、事件重放和恢复结果仍然正确。"""

import asyncio
import os
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import create_checkpoint, empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from knowledgeops_agent.app.runs.events import RedisEventBridge, iter_run_events
from knowledgeops_agent.app.runs.service import RunService
from knowledgeops_agent.persistence.checkpoints import CachedPostgresSaver
from knowledgeops_agent.persistence.redis import RedisBlobCache


@pytest.fixture
def redis_case():
    """只删除本例随机前缀，不清空 Redis，也不接触 broker 的 DB 0。"""
    url = os.environ["TEST_REDIS_URL"]
    assert url.endswith("/15")
    client = Redis.from_url(url)
    prefix = "test-runtime:" + uuid4().hex
    try:
        yield url, client, prefix
    finally:
        keys = list(client.scan_iter(match=prefix + ":*"))
        if keys:
            client.unlink(*keys)
        client.close()


def make_jobs(sessions, bridge):
    jobs = RunService(sessions, event_bridge=bridge)
    conversation = jobs.create_conversation("缓存边界")
    run = jobs.enqueue(conversation["id"], "不调用模型", request_key=uuid4().hex)
    return jobs, run


def test_events_publish_only_after_outer_commit_and_not_rollback(business_sessions, redis_case):
    url, client, prefix = redis_case
    bridge = RedisEventBridge(client, url, prefix)
    jobs, run = make_jobs(business_sessions, bridge)
    key = bridge.key(run["id"])
    assert client.xlen(key) == 1
    with business_sessions.begin() as session:
        row = jobs._locked(session, run["id"])
        jobs._event(session, row, "outer", {})
        with session.begin_nested():
            jobs._event(session, row, "nested_commit", {})
        assert client.xlen(key) == 1
        with pytest.raises(ValueError), session.begin_nested():
            jobs._event(session, row, "nested_rollback", {})
            raise ValueError("discard")
    assert client.xlen(key) == 3
    with pytest.raises(ValueError), business_sessions.begin() as session:
        jobs._event(session, jobs._locked(session, run["id"]), "rollback", {})
        raise ValueError("discard")
    assert [row["kind"] for row in jobs.events(run["id"])] == ["queued", "outer", "nested_commit"]
    assert client.xlen(key) == 3
    assert 0 < client.ttl(key) <= bridge.ttl_seconds


async def never_disconnected():
    return False


@pytest.mark.parametrize("redis_broken", [False, True])
async def test_sse_replay_trimmed_missing_events_and_terminal(
    business_sessions, redis_case, monkeypatch, redis_broken
):
    url, client, prefix = redis_case
    bridge = RedisEventBridge(client, url, prefix, max_events=2)
    jobs, run = make_jobs(business_sessions, bridge)
    jobs.claim("offline", run_id=run["id"], generation=run["generation"])
    for i in range(205):
        jobs.append_event(run["id"], "progress", {"step": i})
    jobs.finish(run["id"], "offline")
    if redis_broken:
        monkeypatch.setattr(bridge, "url", "redis://127.0.0.1:1/15")
    parts = [part async for part in iter_run_events(jobs, bridge, run["id"], 2, never_disconnected)]
    ids = [int(part.splitlines()[0].split(": ")[1]) for part in parts if part.startswith("id:")]
    assert ids == list(range(3, jobs.get_run(run["id"])["last_event"] + 1))
    assert parts[-1] == "event: closed\ndata: {}\n\n"


async def test_live_stream_wakes_without_pg_poll_and_repairs_missing_publication(
    business_sessions, redis_case, monkeypatch
):
    url, client, prefix = redis_case
    bridge = RedisEventBridge(client, url, prefix)
    jobs, run = make_jobs(business_sessions, bridge)
    jobs.claim("offline", run_id=run["id"], generation=run["generation"])
    stream = iter_run_events(jobs, bridge, run["id"], 0, never_disconnected, reconcile_seconds=0.1)
    assert "id: 1\n" in await anext(stream)
    waiter = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.03)
    jobs.append_event(run["id"], "model_delta", {"text": "实时文本"})
    # 已持久化的 started 事件可能先返回，然后才是实时文本。
    first_live = await asyncio.wait_for(waiter, 3)
    if "实时文本" not in first_live:
        first_live = await asyncio.wait_for(anext(stream), 3)
    assert "实时文本" in first_live

    # 模拟 PG 提交成功、Redis 发布失败：后续轮询仍能恢复最后一条终态。
    def unavailable(*args, **kwargs):
        raise RedisConnectionError("offline")

    monkeypatch.setattr(client, "pipeline", unavailable)
    jobs.finish(run["id"], "offline")
    rest = [part async for part in stream]
    assert any('"kind": "finished"' in part for part in rest)
    assert rest[-1].startswith("event: closed")


@pytest.fixture
def checkpoint_case(business_sessions, redis_case):
    _, redis, prefix = redis_case
    engine = business_sessions.kw["bind"]
    with engine.connect() as sql:
        schema = sql.exec_driver_sql("SELECT current_schema()").scalar_one()
    args, kwargs = engine.dialect.create_connect_args(engine.url)
    with Connection.connect(
        *args,
        **kwargs,
        options=f"-csearch_path={schema}",
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    ) as conn:
        cache = RedisBlobCache(redis, prefix, ttl_seconds=60, max_entries=8)
        saver = CachedPostgresSaver(conn, cache=cache)
        saver.setup()
        yield saver, PostgresSaver(conn), cache, redis


def save_messages(saver, config, text, version):
    checkpoint = create_checkpoint(empty_checkpoint(), None, version)
    # 基础包的旧 helper 仍返回 v2；当前 LangGraph 实际运行产生 v4。
    checkpoint["v"] = 4
    checkpoint["channel_values"] = {"messages": [HumanMessage(text)], "count": version}
    checkpoint["channel_versions"] = {"messages": str(version), "count": str(version)}
    return saver.put(
        config, checkpoint, {"source": "input", "step": version}, checkpoint["channel_versions"]
    )


def test_cache_matches_pg_and_never_hides_latest_or_pending_writes(checkpoint_case):
    saver, plain, cache, _ = checkpoint_case
    config = {"configurable": {"thread_id": uuid4().hex, "checkpoint_ns": ""}}
    first = save_messages(saver, config, "第一轮", 1)
    assert saver.get_tuple(first) == plain.get_tuple(first)
    misses = cache.stats["misses"]
    blob_queries = saver.blob_queries
    assert saver.get_tuple(first) == plain.get_tuple(first)
    assert cache.stats["hits"] > 0 and cache.stats["misses"] == misses
    assert saver.blob_queries == blob_queries
    saver.put_writes(first, [("messages", [HumanMessage("已经完成的工具结果")])], "task-1")
    assert saver.get_tuple(first) == plain.get_tuple(first)
    second = save_messages(saver, first, "第二轮", 2)
    assert saver.get_tuple(config) == plain.get_tuple(second)
    assert saver.get_tuple(first).checkpoint["channel_values"]["messages"][0].content == "第一轮"
    assert list(saver.list(config)) == list(plain.list(config))
    saver.delete_thread(config["configurable"]["thread_id"])
    assert saver.get_tuple(first) is None
    assert not list(
        cache.client.scan_iter(match=cache.thread_prefix(config["configurable"]["thread_id"]) + "*")
    )


def test_cache_loss_corruption_and_outage_fall_back_to_pg(checkpoint_case, monkeypatch):
    saver, plain, cache, redis = checkpoint_case
    config = {"configurable": {"thread_id": uuid4().hex, "checkpoint_ns": ""}}
    saved = save_messages(saver, config, "保留原文", 1)
    expected = plain.get_tuple(saved)
    assert saver.get_tuple(saved) == expected
    keys = list(
        redis.scan_iter(match=cache.thread_prefix(config["configurable"]["thread_id"]) + "*")
    )
    redis.set(keys[0], b"broken")
    assert saver.get_tuple(saved) == expected
    redis.unlink(*keys)
    assert saver.get_tuple(saved) == expected

    def unavailable(*args, **kwargs):
        raise RedisConnectionError("offline")

    monkeypatch.setattr(redis, "mget", unavailable)
    assert saver.get_tuple(saved) == expected


def test_cache_bound_and_database_namespace_isolation(redis_case):
    _, client, prefix = redis_case
    cache = RedisBlobCache(client, prefix, max_entries=2, max_value_bytes=100)
    other = RedisBlobCache(client, prefix + ":other", max_entries=2)
    cache.set_many({"t:one": b"a", "t:two": b"b", "t:three": b"c", "t:large": b"x" * 101})
    assert len(cache.get_many(["t:one", "t:two", "t:three", "t:large"])) == 2
    assert other.get_many(["t:three"]) == {}


def test_actual_graph_resume_reuses_committed_branch_writes(checkpoint_case):
    """真实 LangGraph 失败恢复：已完成分支不会因热缓存里的旧状态被重做。"""
    from typing import Annotated, TypedDict

    from langgraph.graph import START, StateGraph
    from langgraph.graph.message import add_messages

    saver, plain, cache, _ = checkpoint_case

    class State(TypedDict):
        messages: Annotated[list, add_messages]

    attempts = {"left": 0, "right": 0}

    def left(state):
        attempts["left"] += 1
        return {"messages": [HumanMessage("左侧完成", id="left")]}

    def right(state):
        attempts["right"] += 1
        if attempts["right"] == 1:
            raise ValueError("模拟右侧失败")
        return {"messages": [HumanMessage("右侧完成", id="right")]}

    builder = StateGraph(State)
    builder.add_node("left", left)
    builder.add_node("right", right)
    builder.add_edge(START, "left")
    builder.add_edge(START, "right")
    graph = builder.compile(checkpointer=saver)
    config = {"configurable": {"thread_id": uuid4().hex}}
    with pytest.raises(ValueError, match="右侧失败"):
        graph.invoke({"messages": [HumanMessage("开始", id="user")]}, config)
    assert saver.get_tuple(config) == plain.get_tuple(config)
    assert saver.get_tuple(config) == plain.get_tuple(config)
    # 新 saver/新图模拟接续者，不沿用前一次图对象里的状态。
    reopened = builder.compile(checkpointer=CachedPostgresSaver(saver.conn, cache=cache))
    result = reopened.invoke(None, config)
    assert attempts == {"left": 1, "right": 2}
    assert {m.id for m in result["messages"]} == {"user", "left", "right"}
    assert cache.stats["hits"] > 0


async def test_live_stream_uses_redis_without_requerying_pg(
    business_sessions, redis_case, monkeypatch
):
    """读完初始页后，热事件直接由跨连接 Redis 发布唤醒，无需半秒 PG 轮询。"""
    url, client, prefix = redis_case
    bridge = RedisEventBridge(client, url, prefix)
    jobs, run = make_jobs(business_sessions, bridge)
    stream = iter_run_events(jobs, bridge, run["id"], 0, never_disconnected, reconcile_seconds=10)
    assert "id: 1\n" in await anext(stream)
    waiter = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.1)  # 让初次 PG 追平检查结束，进入阻塞 XREAD。

    def unexpected_pg_read(*args, **kwargs):
        raise AssertionError("hot event should not poll PG")

    monkeypatch.setattr(jobs, "events", unexpected_pg_read)
    jobs.append_event(run["id"], "model_delta", {"text": "立即展示"})
    try:
        assert "立即展示" in await asyncio.wait_for(waiter, 3)
    finally:
        await stream.aclose()


def test_old_checkpoint_format_uses_official_migration_path(checkpoint_case):
    saver, plain, cache, _ = checkpoint_case
    config = {"configurable": {"thread_id": uuid4().hex, "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": [HumanMessage("旧格式")]}
    checkpoint["channel_versions"] = {"messages": "1"}
    saved = saver.put(config, checkpoint, {"source": "input", "step": 0}, {"messages": "1"})
    assert saver.get_tuple(saved) == plain.get_tuple(saved)
    assert cache.stats["hits"] == 0


async def test_live_redis_reader_outage_still_delivers_completion(business_sessions, redis_case):
    url, client, prefix = redis_case
    bridge = RedisEventBridge(client, url, prefix)
    jobs, run = make_jobs(business_sessions, bridge)
    jobs.claim("offline", run_id=run["id"], generation=run["generation"])
    bridge.url = "redis://127.0.0.1:1/15"
    stream = iter_run_events(jobs, bridge, run["id"], 2, never_disconnected)
    waiter = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.15)
    jobs.finish(run["id"], "offline")
    assert '"kind": "finished"' in await asyncio.wait_for(waiter, 3)
    assert (await anext(stream)).startswith("event: closed")
    await stream.aclose()


async def test_live_sequence_gap_replays_pg_without_skipping(business_sessions, redis_case):
    url, client, prefix = redis_case
    bridge = RedisEventBridge(client, url, prefix, max_events=1)
    jobs, run = make_jobs(business_sessions, bridge)
    stream = iter_run_events(jobs, bridge, run["id"], 0, never_disconnected)
    assert "id: 1\n" in await anext(stream)
    waiter = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.05)
    # 一次事务发布两条，MAXLEN=1 会让订阅者直接看到第三条；必须先补第二条。
    with business_sessions.begin() as session:
        row = jobs._locked(session, run["id"])
        jobs._event(session, row, "progress", {"n": 2})
        jobs._event(session, row, "progress", {"n": 3})
    assert "id: 2\n" in await asyncio.wait_for(waiter, 3)
    assert "id: 3\n" in await anext(stream)
    await stream.aclose()


def test_source_deletion_and_reused_thread_do_not_revive_cached_content(checkpoint_case):
    saver, plain, cache, _ = checkpoint_case
    config = {"configurable": {"thread_id": uuid4().hex, "checkpoint_ns": ""}}
    old = save_messages(saver, config, "已删除的内容", 1)
    saver.get_tuple(old)
    # 模拟源删除成功、缓存清理来不及发生；必须先检查源存在性。
    plain.delete_thread(config["configurable"]["thread_id"])
    assert saver.get_tuple(old) is None
    current = save_messages(saver, config, "重新建立的内容", 1)
    assert saver.get_tuple(current) == plain.get_tuple(current)
    assert (
        saver.get_tuple(current).checkpoint["channel_values"]["messages"][0].content
        == "重新建立的内容"
    )


def test_sse_http_contract_with_redis_enabled(business_sessions, redis_case):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from knowledgeops_agent.app.application import create_app

    url, redis, prefix = redis_case
    bridge = RedisEventBridge(redis, url, prefix)
    jobs, run = make_jobs(business_sessions, bridge)
    jobs.claim("offline", run_id=run["id"], generation=run["generation"])
    jobs.finish(run["id"], "offline")
    services = SimpleNamespace(
        jobs=jobs,
        event_bridge=bridge,
        sessions=business_sessions,
        settings=SimpleNamespace(
            workspace_root=None, api_allowed_hosts="testserver,localhost,127.0.0.1"
        ),
        # 注入服务不持有真实维护租约时，用非 None 占位表示"已由外层持有"，
        # 避免测试去获取正式库的写入保护；中间件仍会调用只读检查。
        maintenance=SimpleNamespace(),
        check_maintenance=lambda: None,
    )
    with TestClient(create_app(services=services, publish_jobs=False)) as client:
        response = client.get(f"/api/runs/{run['id']}/events", headers={"Last-Event-ID": "1"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert "id: 1\n" not in response.text and "id: 2\n" in response.text
    assert response.text.endswith("event: closed\ndata: {}\n\n")
