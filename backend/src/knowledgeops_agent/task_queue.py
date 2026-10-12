"""Celery + Redis 接入与事务外盒，业务状态不依赖 Celery result backend。

Redis 仅传小消息；结果与恢复条件从 PostgreSQL 读取。投递重试和业务
重试严格区分：重投通知可以自动完成，重新运行收费 Agent 必须经过状态门。
"""

from collections.abc import Callable
from contextlib import contextmanager
from datetime import timedelta
from threading import Lock

from celery import Celery
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from knowledgeops_agent.db import Run, RunDispatch, utcnow

TASK_NAME = "knowledgeops.execute_run"


def _execute_run(run_id: str, generation: int):
    """延迟加载运行实现，API 创建生产者时无需加载模型或检查点连接。"""
    from knowledgeops_agent.worker import execute_run

    return execute_run(run_id, generation)


def create_queue(
    broker_url: str, *, handler: Callable | None = None, queue_name: str = "knowledgeops.agent"
) -> Celery:
    """构造生产者/worker 共用配置；创建实例本身不连接 broker 或调用模型。"""
    app = Celery("knowledgeops", broker=broker_url)
    app.conf.update(
        # Celery 的通用 CELERY_BROKER_URL 环境变量优先于构造参数；明确指定
        # 两个连接方向，保证依赖注入的隔离 broker 真正用于发送和消费。
        broker_read_url=broker_url,
        broker_write_url=broker_url,
        task_default_queue=queue_name,
        task_serializer="json",
        accept_content=["json"],
        task_ignore_result=True,
        result_backend=None,
        task_acks_late=True,
        task_reject_on_worker_lost=False,
        worker_prefetch_multiplier=1,
        task_publish_retry=False,  # 外盒负责可观测、可恢复的发送重试。
        broker_connection_timeout=3,
        broker_connection_retry_on_startup=True,
        broker_transport_options={
            "visibility_timeout": 9000,  # 大于应用允许的最长活跃时间，仍需防重复消费。
            "socket_timeout": 3,
            "socket_connect_timeout": 3,
        },
        visibility_timeout=9000,
        timezone="UTC",
        enable_utc=True,
    )
    # shared=False 避免多个测试/独立应用实例相互继承不同的任务实现。
    app.task(name=TASK_NAME, shared=False, ignore_result=True)(handler or _execute_run)
    return app


class OutboxPublisher:
    """扫描待投递记录，短事务内锁定一批；连接失败不丢掉用户请求。

    发送成功但 DB 提交前崩溃会造成重投，消费者按状态/代次去重。已发消息
    若被 broker 丢失，仍在 queued 的任务会定期补发；运行中/已中断的不补发。
    """

    def __init__(self, sessions, queue: Celery):
        self.sessions = sessions
        self.queue = queue

    def publish_due(self, *, limit: int = 10) -> int:
        now = utcnow()
        sent = 0
        with self.sessions.begin() as session:
            pending = session.scalars(
                select(RunDispatch)
                .join(Run, Run.id == RunDispatch.run_id)
                .where(
                    Run.status == "queued",
                    Run.cancel_requested.is_(False),
                    Run.generation == RunDispatch.generation,
                    RunDispatch.next_attempt_at <= now,
                )
                .order_by(RunDispatch.next_attempt_at, RunDispatch.id)
                .with_for_update(of=RunDispatch, skip_locked=True)
                .limit(max(1, min(limit, 20)))
            )
            for dispatch in pending:
                dispatch.attempts += 1
                try:
                    self.queue.send_task(
                        TASK_NAME,
                        args=[dispatch.run_id, dispatch.generation],
                        task_id=f"{dispatch.run_id}:{dispatch.generation}",
                    )
                except Exception as exc:
                    # 异常正文可能包含 broker DSN，只保留类型；运行详情能看见投递尚未成功。
                    dispatch.last_error = type(exc).__name__
                    dispatch.next_attempt_at = now + timedelta(
                        seconds=min(30, 2 ** min(dispatch.attempts, 5))
                    )
                else:
                    dispatch.published_at, dispatch.last_error = now, None
                    dispatch.next_attempt_at = now + timedelta(seconds=30)
                    sent += 1
        return sent


class WorkerLease:
    """检查取得锁的原数据库会话仍然存在；重连的新连接不能冒充旧租约。"""

    def __init__(self, connection, key: int):
        self.connection, self.key = connection, key
        self.backend_pid = connection.scalar(text("SELECT pg_backend_pid()"))
        self._lock, self._lost = Lock(), False

    def check(self) -> None:
        with self._lock:
            if self._lost:
                raise RuntimeError("worker lease was lost")
            try:
                owned = self.connection.scalar(
                    text(
                        "SELECT pg_backend_pid() = :pid AND EXISTS ("
                        "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                        "AND pid = pg_backend_pid() AND granted AND objsubid = 1 "
                        "AND classid::bigint = ((CAST(:key AS bigint) >> 32) & 4294967295) "
                        "AND objid::bigint = (CAST(:key AS bigint) & 4294967295))"
                    ),
                    {"pid": self.backend_pid, "key": self.key},
                )
                if not owned:
                    raise RuntimeError("worker lease no longer owned")
            except Exception:
                self._lost = True
                raise RuntimeError("worker lease was lost") from None


@contextmanager
def worker_lease(engine):
    """首版只容许运行一个根任务 worker；会话锁随进程连接断开释放。

    租约连接必须一直保持。worker 取得租约后才可把上一实例遗留运行标为
    interrupted；不能由每个 Celery 子进程分别执行启动恢复。
    """
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        # 同一业务 schema 只允许一个 worker；隔离测试 schema 不能互相抢占租约。
        lease_key = connection.scalar(
            text(
                "SELECT hashtextextended(concat_ws('/', current_database(), current_schema(), 'knowledgeops-worker'), 0)"
            )
        )
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lease_key})
        if not acquired:
            raise RuntimeError("another KnowledgeOps worker already owns the lease")
        try:
            yield WorkerLease(connection, lease_key)
        finally:
            try:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lease_key})
            except SQLAlchemyError:
                # 连接失效时不要把它放回池中继续承载其他任务。
                connection.invalidate()
