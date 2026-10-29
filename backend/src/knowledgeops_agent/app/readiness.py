"""分层就绪探测与任务阻塞原因。

只读：不发送、领取、恢复任务，不调用模型，也不启动 MCP。每项探测独立有界，
整份结果短缓存，避免页面轮询放大依赖压力；Worker 忙碌不等于离线。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from knowledgeops_agent.core.config import PROJECT_ROOT
from knowledgeops_agent.persistence.models import Run
from knowledgeops_agent.persistence.redis import redis_client

CACHE_SECONDS = 3.0
HEARTBEAT_STALE_SECONDS = 10
STATEMENT_TIMEOUT_MS = 2000

# 与 worker_lease() 完全相同的键计算：64 位 advisory lock 在 pg_locks 中
# 分为 classid/objid 两段（objsubid=1）。只读查询锁，不尝试加锁，
# 避免探测本身与正在启动的 worker 争抢独占租约。
WORKER_LEASE = text(
    """
    WITH key AS (
        SELECT hashtextextended(
            concat_ws('/', current_database(), current_schema(), 'knowledgeops-worker'), 0
        ) AS value
    )
    SELECT count(*) FROM pg_locks, key
    WHERE locktype = 'advisory' AND objsubid = 1
      AND classid::bigint = ((key.value >> 32) & 4294967295)
      AND objid::bigint = (key.value & 4294967295)
    """
)

# 领取规则的部分条件与 claim() 保持一致；这里只解释原因，不改变调度。
ACTIVE_ROOT = text(
    """
    SELECT root_id FROM runs
    WHERE status IN ('running', 'waiting_children')
      AND root_id <> :root_id
    ORDER BY created_at, id LIMIT 1
    """
)
PENDING_IN_CONVERSATION = text(
    """
    SELECT id FROM runs
    WHERE conversation_id = :conversation_id AND parent_id IS NULL AND id <> :run_id
      AND (status IN ('paused', 'waiting_input', 'waiting_children')
           OR (status = 'interrupted'
               AND outcome->>'kind' IN ('waiting_approval', 'execution_unknown')))
    ORDER BY created_at, id LIMIT 1
    """
)
EARLIER_QUEUED = text(
    """
    SELECT id FROM runs
    WHERE conversation_id = :conversation_id AND parent_id IS NULL AND id <> :run_id
      AND status = 'queued' AND origin = 'human'
      AND conversation_sequence IS NOT NULL
      AND conversation_sequence < :sequence
    ORDER BY conversation_sequence LIMIT 1
    """
)

PROCESS_STATES = {
    "queued": ("排队中", "等待 Worker 领取"),
    "running": ("正在执行", "完成后可在运行记录查看结果"),
    "waiting_children": ("等待子任务", "子任务完成后根任务继续"),
    "paused": ("已暂停", "在原任务页手动继续"),
    "waiting_input": ("等待回答", "在原任务页回答后可继续"),
    "completed": ("已结束", "结果已保存"),
    "failed": ("失败", "可在原任务页核对后恢复"),
    "interrupted": ("已中断", "按原任务页提示人工恢复"),
    "cancelled": ("已取消", "任务不再执行"),
    "budget_exhausted": ("预算用尽", "需要人工决定是否恢复"),
}


def _probe(callable_):
    """单项探测失败只降级该项，不抛给整份响应。"""
    try:
        value = callable_()
        return {"ok": True, **({"detail": value} if isinstance(value, str) else (value or {}))}
    except Exception as exc:  # noqa: BLE001 - 探针只报告类型，避免把连接串写进响应
        return {"ok": False, "detail": type(exc).__name__}


class Readiness:
    def __init__(self, services):
        self.services = services
        self._cache: dict | None = None
        self._cached_at = 0.0
        self._receipt_engine = None

    # ---- 基础就绪 ----

    def _database(self):
        with self.services.sessions() as session:
            session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'"))
            session.execute(text("SELECT 1"))
        return {"detail": "业务库可连接"}

    def _migration(self):
        if PROJECT_ROOT is None:
            return {"ok": False, "detail": "源码目录不可用", "current": None, "head": None}
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config(str(Path(PROJECT_ROOT) / "alembic.ini"))
        config.set_main_option("script_location", str(Path(PROJECT_ROOT) / "migrations"))
        head = ScriptDirectory.from_config(config).get_current_head()
        try:
            with self.services.sessions() as session:
                session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'"))
                current = session.scalar(text("SELECT version_num FROM alembic_version"))
        except SQLAlchemyError:
            # 未通过 Alembic 初始化的库（例如只用 models 建表的隔离 schema）
            # 不能冒充"版本一致"，必须如实报告为未就绪。
            return {"ok": False, "detail": "迁移版本表缺失", "current": None, "head": head}
        return {"ok": current == head, "current": current, "head": head}

    def _receipts(self):
        url = self.services.settings.accounting_database_url
        if not url:
            return {"ok": False, "detail": "未配置回执库"}
        if self._receipt_engine is None:
            connect_args = {"connect_timeout": 2} if url.startswith("postgresql") else {}
            self._receipt_engine = create_engine(
                url, pool_pre_ping=True, pool_size=1, max_overflow=0, connect_args=connect_args
            )
        with self._receipt_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {}

    def _directories(self):
        def check(name, path):
            if path is None:
                return {f"{name}_ok": False, f"{name}_path": ""}
            resolved = Path(path)
            return {
                f"{name}_ok": resolved.is_dir() and os.access(resolved, os.W_OK),
                f"{name}_path": str(resolved),
            }

        settings = self.services.settings
        skills = Path(os.environ.get("SKILLS_DIRECTORY") or (Path(PROJECT_ROOT) / "skills")) if PROJECT_ROOT else None
        mcp = Path(os.environ.get("MCP_DIRECTORY") or (Path(PROJECT_ROOT) / "mcp")) if PROJECT_ROOT else None
        items = {}
        items.update(check("workspace", settings.workspace_root))
        items.update(check("skills", skills))
        items.update(check("mcp", mcp))
        return {
            "ok": all(value for key, value in items.items() if key.endswith("_ok")),
            "items": items,
        }

    # ---- 执行就绪 ----

    def _execution(self):
        with self.services.sessions() as session:
            session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'"))
            lease = int(session.scalar(WORKER_LEASE) or 0) > 0
            active = int(session.scalar(text("SELECT count(*) FROM runs WHERE status = 'running'")) or 0)
            age = session.scalar(
                text(
                    "SELECT EXTRACT(EPOCH FROM now() - max(heartbeat_at))::int FROM runs "
                    "WHERE status = 'running' AND heartbeat_at IS NOT NULL"
                )
            )
        redis = {}
        for name, url in (("broker", self.services.settings.celery_broker_url),
                          ("runtime", self.services.settings.runtime_redis_url)):
            if not url:
                redis[name] = {"ok": False, "detail": "未配置"}
                continue
            client = redis_client(url)
            try:
                client.ping()
                redis[name] = {"ok": True}
            except Exception as exc:  # noqa: BLE001
                redis[name] = {"ok": False, "detail": type(exc).__name__}
            finally:
                client.close()
        if not lease:
            state = "offline"
        elif active > 0:
            state = "busy"
        else:
            state = "ready"
        return {
            "state": state,
            "worker_lease": lease,
            "active_runs": active,
            "heartbeat_seconds": int(age) if age is not None else None,
            "redis": redis,
        }

    def _backlog(self):
        with self.services.sessions() as session:
            session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'"))
            return {
                "queued_runs": int(session.scalar(text("SELECT count(*) FROM runs WHERE status = 'queued'")) or 0),
                "due_dispatches": int(session.scalar(text(
                    "SELECT count(*) FROM run_dispatches WHERE published_at IS NULL AND next_attempt_at <= now()"
                )) or 0),
                "retrying_dispatches": int(session.scalar(text(
                    "SELECT count(*) FROM run_dispatches WHERE published_at IS NULL AND attempts > 0"
                )) or 0),
            }

    # ---- 汇总 ----

    def summary(self) -> dict:
        now = time.monotonic()
        if self._cache is not None and now - self._cached_at < CACHE_SECONDS:
            return {**self._cache, "cached": True}
        database = _probe(self._database)
        migration = _probe(self._migration)
        receipts = _probe(self._receipts)
        directories = _probe(self._directories)
        foundation_ok = all(item["ok"] for item in (database, migration, receipts, directories))
        execution = _probe(self._execution)
        backlog = _probe(self._backlog)
        if not foundation_ok:
            status = "blocked"
        elif execution.get("state") in ("ready", "busy"):
            status = "ready"
        else:
            status = "degraded"
        result = {
            "status": status,
            "checked_at": time.time(),
            "layers": {
                "process": {"ok": True, "detail": "API 进程可响应"},
                "foundation": {
                    "ok": foundation_ok,
                    "database": database,
                    "migration": migration,
                    "receipts": receipts,
                    "directories": directories,
                },
                "execution": execution,
                "backlog": backlog,
            },
        }
        self._cache, self._cached_at = result, now
        return {**result, "cached": False}

    # ---- 单个任务的阻塞原因 ----

    def blocking_reason(self, run_id: str) -> dict:
        with self.services.sessions() as session:
            session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'"))
            run = session.get(Run, run_id)
            if run is None:
                raise KeyError("run not found")
            if run.status != "queued":
                label, action = PROCESS_STATES.get(run.status, (run.status, ""))
                return {"id": run.id, "status": run.status, "claimable": False,
                        "reason": run.status, "detail": label, "action": action, "href": f"/runs/{run.id}"}
            lease = int(session.scalar(WORKER_LEASE) or 0) > 0
            active_root = session.scalar(ACTIVE_ROOT, {"root_id": run.root_id})
            if active_root:
                return {"id": run.id, "status": run.status, "claimable": False,
                        "reason": "root_group_busy", "detail": "其他根任务组正在执行，当前只允许一个活跃根任务组",
                        "href": f"/runs/{run.id}"}
            if run.conversation_id:
                pending = session.scalar(PENDING_IN_CONVERSATION,
                                        {"conversation_id": run.conversation_id, "run_id": run.id})
                if pending:
                    return {"id": run.id, "status": run.status, "claimable": False,
                            "reason": "conversation_pending", "detail": "同会话有未决任务，需先处理后再继续",
                            "href": f"/chat/{run.conversation_id}"}
                if run.conversation_sequence is not None:
                    earlier = session.scalar(EARLIER_QUEUED, {
                        "conversation_id": run.conversation_id, "run_id": run.id,
                        "sequence": run.conversation_sequence,
                    })
                    if earlier:
                        return {"id": run.id, "status": run.status, "claimable": False,
                                "reason": "conversation_earlier_queued",
                                "detail": "同会话前序任务仍在排队，按原有顺序执行",
                                "href": f"/chat/{run.conversation_id}"}
            if not lease:
                return {"id": run.id, "status": run.status, "claimable": False,
                        "reason": "worker_offline", "detail": "后台 Worker 未就绪，任务已保存，不会丢失",
                        "href": f"/runs/{run.id}"}
            return {"id": run.id, "status": run.status, "claimable": True,
                    "reason": "claimable", "detail": "满足当前领取条件，等待 Worker 领取",
                    "href": f"/runs/{run.id}"}
