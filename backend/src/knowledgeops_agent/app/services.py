"""应用依赖的组合根；入口构造依赖，业务代码不从全局环境寻找连接。"""

from dataclasses import dataclass
from functools import cached_property

from redis import Redis
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from knowledgeops_agent.app.lifecycle.catalog import install_reference_tracking
from knowledgeops_agent.app.runs.events import RedisEventBridge
from knowledgeops_agent.app.runs.service import RunService
from knowledgeops_agent.core.config import Settings, create_gateway
from knowledgeops_agent.harness.models.gateway import ModelGateway
from knowledgeops_agent.harness.models.reasoning import ReasoningService
from knowledgeops_agent.knowledge.service import KnowledgeService
from knowledgeops_agent.persistence.files import FileStore
from knowledgeops_agent.persistence.maintenance import MaintenanceLease
from knowledgeops_agent.persistence.redis import RedisBlobCache, database_namespace, redis_client
from knowledgeops_agent.sources.service import SourceService


@dataclass
class Services:
    settings: Settings
    engine: Engine
    sessions: sessionmaker
    jobs: RunService
    knowledge: KnowledgeService
    sources: SourceService
    gateway: ModelGateway
    runtime_redis: Redis | None = None
    event_bridge: RedisEventBridge | None = None
    checkpoint_cache: RedisBlobCache | None = None
    reasoning: ReasoningService | None = None
    maintenance: MaintenanceLease | None = None

    def __post_init__(self):
        # 显式注入的应用实例同样使用事务内引用合同，不能只在默认构造函数生效。
        self.sessions.configure(info={**self.sessions.kw.get("info", {}),
                                      "source_store": FileStore(self.sources.storage_root)})
        install_reference_tracking(self.sessions)

    @cached_property
    def lifecycle(self):
        """维护协调器惰性装配；构造Services不会清理数据或打开检查点连接。"""
        from knowledgeops_agent.app.lifecycle.maintenance import LifecycleMaintenance

        return LifecycleMaintenance.from_services(self)

    @cached_property
    def readiness(self):
        """分层就绪探测与阻塞原因；只读并短缓存，不领取任务或调用模型。"""
        from knowledgeops_agent.app.readiness import Readiness

        return Readiness(self)

    @cached_property
    def backup_records(self):
        """备份与恢复记录的只读投影；网页不控制 Docker，也不下载 dump。"""
        from knowledgeops_agent.app.backup_records import BackupRecords

        return BackupRecords()

    def close(self) -> None:
        """只释放本进程连接池，不停止容器或删除数据。"""
        try:
            self.jobs.close_mcp_sessions()
            self.engine.dispose()
            self.gateway.ledger.engine.dispose()
            if self.runtime_redis is not None:
                self.runtime_redis.close()
        finally:
            if self.maintenance is not None:
                self.maintenance.close()

    def check_maintenance(self) -> None:
        if self.maintenance is not None:
            self.maintenance.check()


def create_services(settings: Settings) -> Services:
    """构造 API/worker 一致的业务边界；不创建业务表，部署须先执行迁移。"""
    if not settings.database_url:
        raise ValueError("DATABASE_URL is required")
    engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=8,
                           connect_args={"connect_timeout": 5})
    # 网关会初始化回执表，FileStore会创建目录，因此保护必须先于依赖构造。
    maintenance = None
    try:
        maintenance = MaintenanceLease(engine, workspace=settings.workspace_root).acquire()
        return _create_services(settings, engine, maintenance)
    except BaseException:
        if maintenance is not None:
            maintenance.close()
        engine.dispose()
        raise


def _create_services(settings: Settings, engine: Engine, maintenance: MaintenanceLease) -> Services:
    store = FileStore(settings.workspace_root) if settings.workspace_root else None
    reasoning = ReasoningService(settings)
    sessions = sessionmaker(
        engine,
        expire_on_commit=False,
        info={"file_store": store, "freeze_reasoning": reasoning.freeze_local},
    )
    install_reference_tracking(sessions)
    client, bridge, cache = None, None, None
    if settings.runtime_redis_url:
        prefix = database_namespace(engine)
        client = redis_client(settings.runtime_redis_url)
        if settings.redis_events_enabled:
            bridge = RedisEventBridge(
                client,
                settings.runtime_redis_url,
                prefix,
                max_events=settings.redis_event_max_entries,
                ttl_seconds=settings.redis_event_ttl_seconds,
            )
        if settings.redis_checkpoint_cache_enabled:
            cache = RedisBlobCache(
                client,
                prefix,
                ttl_seconds=settings.redis_checkpoint_ttl_seconds,
                max_entries=settings.redis_checkpoint_max_entries,
                max_value_bytes=settings.redis_checkpoint_max_value_bytes,
            )
    services = Services(
        settings=settings,
        engine=engine,
        sessions=sessions,
        jobs=RunService(
            sessions,
            event_bridge=bridge,
            mcp_session_options={
                "max_connections": settings.mcp_session_max_connections,
                "idle_seconds": settings.mcp_session_idle_seconds,
                "sweep_seconds": settings.mcp_session_sweep_seconds,
                "close_seconds": settings.mcp_session_close_seconds,
            },
        ),
        knowledge=KnowledgeService(sessions),
        sources=SourceService(sessions, settings.source_storage_root),
        gateway=create_gateway(settings),
        runtime_redis=client,
        event_bridge=bridge,
        checkpoint_cache=cache,
        reasoning=reasoning,
        maintenance=maintenance,
    )
    services.jobs.live_check = services.check_maintenance
    return services
