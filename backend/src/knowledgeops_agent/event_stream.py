"""PG 事件事实 + Redis Streams 实时桥；SSE 游标仍是已有业务事件序号。

参考 DeerFlow 0fe6e63a runtime/stream_bridge/redis.py 的 XADD/XREAD、保留窗口
和 TTL。适配点：本项目已有事务事件日志，缺口直接从 PG 补齐，无需让前端
理解另一种 Redis 游标或 gap 协议，也不把瞬时通知当作执行成功的依据。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError, ResponseError
from redis.retry import Retry
from sqlalchemy import event as sa_event

from knowledgeops_agent.jobs import TERMINAL

logger = logging.getLogger(__name__)


class RedisEventBridge:
    """PG 提交后尽力发布，失败时不回滚已经完成的业务。

    使用业务序号作为 Stream ID，重复/乱序提交产生的空缺由 PG 日志补齐。
    事件有条数、单条大小和过期上限；大事件直接走 PG，不挤占 broker 内存。
    """

    def __init__(
        self,
        client: Redis,
        url: str,
        prefix: str,
        *,
        max_events: int = 1024,
        ttl_seconds: int = 3600,
        max_event_bytes: int = 65536,
    ):
        self.client, self.url, self.prefix = client, url, prefix
        self.max_events, self.ttl_seconds = max_events, ttl_seconds
        self.max_event_bytes = max_event_bytes
        self.unavailable_until = 0.0

    def key(self, run_id: str) -> str:
        return f"{self.prefix}:events:{run_id}"

    def stage(self, session, row) -> None:
        """绑定实际事务；嵌套提交只合并，回滚的事件绝不进入 Redis。"""
        marker = "knowledgeops_event_bridge"
        if marker not in session.info:
            pending: dict[Any, list] = {}
            session.info[marker] = pending

            def committed(current):
                transaction = current.get_nested_transaction() or current.get_transaction()
                rows = pending.pop(transaction, [])
                if transaction is not None and transaction.nested:
                    pending.setdefault(transaction.parent, []).extend(rows)
                else:
                    self.publish(rows)

            def rolled_back(_current, previous_transaction):
                pending.pop(previous_transaction, None)

            def ended(_current, transaction):
                if transaction.parent is None:
                    pending.clear()

            sa_event.listen(session, "after_commit", committed)
            sa_event.listen(session, "after_soft_rollback", rolled_back)
            sa_event.listen(session, "after_transaction_end", ended)
        transaction = session.get_nested_transaction() or session.get_transaction()
        session.info[marker].setdefault(transaction, []).append(row)

    def publish(self, rows: list) -> None:
        if not rows or time.monotonic() < self.unavailable_until:
            return
        try:
            with self.client.pipeline(transaction=True) as pipe:
                for row in rows:
                    payload = json.dumps(
                        {
                            "sequence": row.sequence,
                            "kind": row.kind,
                            "data": row.data,
                            "created_at": row.created_at.isoformat(),
                        },
                        ensure_ascii=False,
                    )
                    if len(payload.encode()) > self.max_event_bytes:
                        continue
                    pipe.xadd(
                        self.key(row.run_id),
                        {"event": payload},
                        id=f"{row.sequence}-0",
                        maxlen=self.max_events,
                        approximate=False,
                    )
                    pipe.expire(self.key(row.run_id), self.ttl_seconds)
                # 某条较旧事件迟到时不能使已提交的业务报错。后续缺口由 PG 修复。
                replies = pipe.execute(raise_on_error=False)
                for reply in replies:
                    if isinstance(reply, ResponseError):
                        if "equal or smaller" in str(reply):
                            continue
                        raise reply
        except RedisError as error:
            self.unavailable_until = time.monotonic() + 5
            logger.warning(
                "event bridge unavailable; SSE will replay PostgreSQL (%s)", type(error).__name__
            )

    def reader(self) -> AsyncRedis:
        """每个 SSE 连接持有独立的阻塞读连接，断开时关闭，不占同步业务线程。"""
        return AsyncRedis.from_url(
            self.url,
            socket_connect_timeout=0.25,
            socket_timeout=1.5,
            retry=Retry(NoBackoff(), 0),
            max_connections=1,
        )


def _sse(row: dict) -> str:
    return (
        f"id: {row['sequence']}\nevent: progress\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
    )


async def iter_run_events(
    jobs,
    bridge: RedisEventBridge | None,
    run_id: str,
    cursor: int,
    disconnected: Callable,
    *,
    reconcile_seconds: float = 5.0,
):
    """初次连接从 PG 回放，追平后阻塞读 Redis；保留定期核对和降级轮询。

    Redis 中只接受连续的下一序号。裁剪、丢失发布、乱序、重启与格式错误都会
    回到 PG，最后一条通知丢失也会被周期核对发现。浏览器断开只关闭订阅，
    不取消 Agent；只有 PG 终态且所有事件已发送才关闭 SSE。
    """
    next_check = 0.0
    last_heartbeat = time.monotonic()
    retry_at = 0.0
    reader = bridge.reader() if bridge else None
    stream_key = bridge.key(run_id) if bridge else ""
    try:
        while not await disconnected():
            now = time.monotonic()
            if now >= next_check:
                rows = await asyncio.to_thread(jobs.events, run_id, after=cursor)
                for row in rows:
                    cursor = row["sequence"]
                    yield _sse(row)
                current = await asyncio.to_thread(jobs.get_run, run_id)
                if current["status"] in TERMINAL and cursor >= current["last_event"]:
                    yield "event: closed\ndata: {}\n\n"
                    return
                # 还有未发出的持久事件时立即读下一页，不额外等待 Redis。
                if cursor < current["last_event"]:
                    continue
                next_check = time.monotonic() + (
                    reconcile_seconds if reader and now >= retry_at else 0.5
                )
            if reader is None or time.monotonic() < retry_at:
                await asyncio.sleep(0.5)
            else:
                try:
                    result = await reader.xread(
                        {stream_key: f"{cursor}-0"},
                        count=200,
                        block=1000,
                    )
                    for _key, entries in result:
                        for event_id, fields in entries:
                            row = json.loads(fields[b"event"])
                            # 同时核对 Stream ID 和正文，缓存异常不能跳过事件。
                            expected = f"{cursor + 1}-0".encode()
                            if event_id != expected or row["sequence"] != cursor + 1:
                                next_check = 0
                                break
                            if not isinstance(row["kind"], str) or not isinstance(
                                row["data"], dict
                            ):
                                raise ValueError("invalid stream event")
                            cursor = row["sequence"]
                            yield _sse(row)
                            if (
                                row["kind"] in {"finished", "interrupted"}
                                or row["data"].get("status") in TERMINAL
                            ):
                                next_check = 0
                except (RedisError, ValueError, KeyError, TypeError):
                    # 不记录原始消息/URL；降级保留旧轮询体验，稍后再尝试 Redis。
                    retry_at = time.monotonic() + 5
                    next_check = 0
            if time.monotonic() - last_heartbeat >= 10:
                yield ": keepalive\n\n"
                last_heartbeat = time.monotonic()
    finally:
        if reader is not None:
            await reader.aclose()
