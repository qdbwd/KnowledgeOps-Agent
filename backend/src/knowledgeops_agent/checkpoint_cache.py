"""PostgresSaver 的版本化通道缓存，正式检查点及 pending_writes 始终保存在 PG。

DeerFlow CachedHistorySaver 只缓存已封存的 delta 历史、不缓存 latest 解析。
本项目使用普通快照，沿用这一不变量，但缓存对象是 checkpoint_blobs 中按
通道版本不可变的消息/状态块。写入、迁移、历史列表及序列化继续使用官方
langgraph-checkpoint-postgres 3.x；这里只适配 get_tuple 的分批读取路径。
"""

from __future__ import annotations

import hashlib
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointTuple, get_checkpoint_id
from langgraph.checkpoint.postgres import PostgresSaver

from knowledgeops_agent.redis_support import RedisBlobCache

# 与官方 SELECT_SQL 的头部/pending_writes 合同一致，但不预先读取全部大 blob。
# 这部分依赖锁定的 PG saver 表结构，升级时须执行缓存/未缓存等价与恢复测试。
_HEADER_SQL = """
SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
       checkpoint, metadata,
       (SELECT array_agg(array[cw.task_id::text::bytea, cw.channel::bytea,
                              cw.type::bytea, cw.blob] ORDER BY cw.task_id, cw.idx)
          FROM checkpoint_writes cw
         WHERE cw.thread_id = checkpoints.thread_id
           AND cw.checkpoint_ns = checkpoints.checkpoint_ns
           AND cw.checkpoint_id = checkpoints.checkpoint_id) AS pending_writes
FROM checkpoints
"""


def _pack(kind: bytes, blob: bytes | None) -> bytes:
    """保留官方 typed-blob；摘要用于检测缓存截断/损坏，不引入 pickle。"""
    payload = kind + b"\0" + (blob or b"")
    return hashlib.sha256(payload).digest() + payload


def _unpack(value: bytes) -> tuple[bytes, bytes] | None:
    digest, payload = value[:32], value[32:]
    if hashlib.sha256(payload).digest() != digest or b"\0" not in payload:
        return None
    kind, blob = payload.split(b"\0", 1)
    return kind, blob


class CachedPostgresSaver(PostgresSaver):
    """保持官方 saver 的写入/恢复接口，仅减少重复通道从 PG 传出的字节数。

    每次先查询 PG 的真实检查点头和未完成写入，随后按其 channel_versions
    读取缓存。Redis 不存在、不可用或内容损坏均从 PG 补读，不能以缓存提供
    一个已经删除的检查点，也不能用旧 pending_writes 重复执行工具。
    """

    def __init__(self, conn, *, cache: RedisBlobCache):
        super().__init__(conn)
        self.cache = cache
        self.blob_queries = 0
        self.blob_bytes = 0

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        values = config["configurable"]
        thread_id = str(values["thread_id"])
        namespace = values.get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)
        args: tuple[Any, ...]
        if checkpoint_id:
            where = " WHERE thread_id=%s AND checkpoint_ns=%s AND checkpoint_id=%s"
            args = (thread_id, namespace, checkpoint_id)
        else:
            where = " WHERE thread_id=%s AND checkpoint_ns=%s ORDER BY checkpoint_id DESC LIMIT 1"
            args = (thread_id, namespace)
        # 复用父类的连接锁，保持同步 worker/子任务共用 saver 的串行读写合同。
        with self._cursor() as cursor:
            cursor.execute(_HEADER_SQL + where, args)
            row = cursor.fetchone()
            if row is None:
                return None
            if row["checkpoint"]["v"] >= 4:
                row["channel_values"] = self._channel_blobs(cursor, row)
                return self._load_checkpoint_tuple(row)
        # 老格式的 pending sends 迁移仍交给官方实现；退出锁后才调用父类。
        return super().get_tuple(config)

    def _channel_blobs(self, cursor, row: dict[str, Any]) -> list:
        checkpoint = row["checkpoint"]
        # PG saver 3.x 将简单标量直接放在头部，只有复杂值需要读取二进制。
        versions = {
            channel: str(version)
            for channel, version in checkpoint["channel_versions"].items()
            if channel not in (checkpoint.get("channel_values") or {})
        }
        keys = {
            channel: self.cache.key(
                row["thread_id"], row["checkpoint_ns"], row["checkpoint_id"], channel, version
            )
            for channel, version in versions.items()
        }
        cached = self.cache.get_many(list(keys.values()))
        blobs, missing = [], []
        for channel, key in keys.items():
            value = _unpack(cached[key]) if key in cached else None
            if value is None:
                missing.append(channel)
            else:
                blobs.append((channel.encode(), *value))
        if missing:
            self.blob_queries += 1
            cursor.execute(
                """SELECT bl.channel, bl.type, bl.blob FROM checkpoint_blobs bl
                   JOIN unnest(%s::text[], %s::text[]) AS wanted(channel, version)
                     ON bl.channel=wanted.channel AND bl.version=wanted.version
                   WHERE bl.thread_id=%s AND bl.checkpoint_ns=%s""",
                (missing, [versions[ch] for ch in missing], row["thread_id"], row["checkpoint_ns"]),
            )
            fill = {}
            for item in cursor.fetchall():
                kind, blob = item["type"].encode(), item["blob"]
                blobs.append((item["channel"].encode(), kind, blob))
                self.blob_bytes += len(blob or b"")
                fill[keys[item["channel"]]] = _pack(kind, blob)
            self.cache.set_many(fill)
        return blobs

    def delete_thread(self, thread_id: str) -> None:
        super().delete_thread(thread_id)
        self.cache.delete_thread(thread_id)
