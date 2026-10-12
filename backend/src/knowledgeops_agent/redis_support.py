"""运行加速设施：独立于 Celery broker 的命名空间、有界缓存和快速失效回退。

借鉴 DeerFlow 0fe6e63a 的 checkpoint_cache：按数据库隔离、批量读取、TTL、
Redis 错误视为未命中。此处适配同步 worker，缓存 PG 的不可变通道二进制，
不复制上游仅适用于异步 DeltaChannel 的历史合成器。
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import cast

from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

logger = logging.getLogger(__name__)


def redis_client(url: str) -> Redis:
    """缓存请求不自动重试；短超时避免 Redis 故障拖住 PG 业务。"""
    return Redis.from_url(
        url,
        socket_connect_timeout=0.25,
        socket_timeout=0.25,
        retry=Retry(NoBackoff(), 0),
        max_connections=16,
    )


def database_namespace(engine) -> str:
    """隔离正常库、恢复库和测试 schema；键中不包含凭据或原始 DSN。"""
    with engine.connect() as connection:
        identity = connection.exec_driver_sql("SELECT current_database(), current_schema()").one()
    location = [engine.url.host, engine.url.port, *identity]
    return "knowledgeops:runtime:v1:" + hashlib.sha256(repr(location).encode()).hexdigest()[:24]


class RedisBlobCache:
    """只读穿透缓存。TTL/容量只决定命中率，不能代替 PG 的存在性判断。

    按命名空间限制条目数和单条字节数，避免与 broker 共用 Redis 服务时采用
    全局 LRU 淘汰队列。淘汰过程跨进程可能保守多删缓存，但不改变业务结果。
    发生连接错误后短暂跳过请求；所有异常日志只记录类型，不输出状态正文。
    """

    def __init__(
        self,
        client: Redis,
        prefix: str,
        *,
        ttl_seconds: int = 900,
        max_entries: int = 128,
        max_value_bytes: int = 2 * 1024 * 1024,
    ):
        self.client = client
        self.prefix = prefix + ":checkpoint:"
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.max_value_bytes = max_value_bytes
        self.index_key = prefix + ":checkpoint-order"
        self.stats = {"hits": 0, "misses": 0, "errors": 0}
        self.unavailable_until = 0.0

    def thread_prefix(self, thread_id: str) -> str:
        return self.prefix + hashlib.sha256(thread_id.encode()).hexdigest() + ":"

    def key(
        self,
        thread_id: str,
        namespace: str,
        checkpoint_id: str,
        channel: str,
        version: str,
    ) -> str:
        # 同一 thread 被删除后重建时，即使某通道重用了版本号，也不命中旧检查点。
        suffix = hashlib.sha256(
            repr((namespace, checkpoint_id, channel, version)).encode()
        ).hexdigest()
        return self.thread_prefix(thread_id) + suffix

    def _failed(self, error: RedisError) -> None:
        self.stats["errors"] += 1
        self.unavailable_until = time.monotonic() + 5
        logger.warning("checkpoint cache unavailable; using PostgreSQL (%s)", type(error).__name__)

    def get_many(self, keys: list[str]) -> dict[str, bytes]:
        if not keys:
            return {}
        found = {}
        if time.monotonic() >= self.unavailable_until:
            try:
                values = cast(
                    list[bytes | None],
                    self.client.mget(
                        [key if key.startswith(self.prefix) else self.prefix + key for key in keys]
                    ),
                )
                found = {
                    key: value for key, value in zip(keys, values, strict=True) if value is not None
                }
            except RedisError as error:
                self._failed(error)
        self.stats["hits"] += len(found)
        self.stats["misses"] += len(keys) - len(found)
        return found

    def set_many(self, values: dict[str, bytes]) -> None:
        if time.monotonic() < self.unavailable_until:
            return
        # 调用者通常传入完整稳定键；简单测试/内部调用也必须使用本缓存前缀。
        values = {
            key if key.startswith(self.prefix) else self.prefix + key: value
            for key, value in values.items()
            if len(value) <= self.max_value_bytes
        }
        if not values:
            return
        try:
            with self.client.pipeline(transaction=True) as pipe:
                for key, value in values.items():
                    pipe.set(key, value, ex=self.ttl_seconds)
                pipe.zadd(self.index_key, {key: time.time() for key in values})
                pipe.expire(self.index_key, self.ttl_seconds)
                pipe.execute()
            stale = cast(list[bytes], self.client.zrange(self.index_key, 0, -self.max_entries - 1))
            if stale:
                with self.client.pipeline(transaction=True) as pipe:
                    pipe.unlink(*stale)
                    pipe.zrem(self.index_key, *stale)
                    pipe.execute()
        except RedisError as error:
            self._failed(error)

    def delete_thread(self, thread_id: str) -> None:
        """正式记录删除后再清理副本；清理失败时 TTL 兜底，绝不复活源数据。"""
        try:
            for key in self.client.scan_iter(match=self.thread_prefix(thread_id) + "*", count=128):
                with self.client.pipeline(transaction=True) as pipe:
                    pipe.unlink(key)
                    pipe.zrem(self.index_key, key)
                    pipe.execute()
        except RedisError as error:
            self._failed(error)
