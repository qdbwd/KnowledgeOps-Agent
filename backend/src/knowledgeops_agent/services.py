"""应用依赖的组合根；入口构造依赖，业务代码不从全局环境寻找连接。"""

from dataclasses import dataclass

from redis import Redis
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from knowledgeops_agent.config import Settings, create_gateway
from knowledgeops_agent.event_stream import RedisEventBridge
from knowledgeops_agent.file_store import FileStore
from knowledgeops_agent.jobs import RunService
from knowledgeops_agent.knowledge import KnowledgeService
from knowledgeops_agent.model_gateway import ModelGateway
from knowledgeops_agent.redis_support import RedisBlobCache, database_namespace, redis_client
from knowledgeops_agent.sources import SourceService


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

    def close(self) -> None:
        """只释放本进程连接池，不停止容器或删除数据。"""
        self.engine.dispose()
        self.gateway.ledger.engine.dispose()
        if self.runtime_redis is not None:
            self.runtime_redis.close()


def create_services(settings: Settings) -> Services:
    """构造 API/worker 一致的业务边界；不创建业务表，部署须先执行迁移。"""
    if not settings.database_url:
        raise ValueError("DATABASE_URL is required")
    engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=8)
    store = FileStore(settings.workspace_root) if settings.workspace_root else None
    sessions = sessionmaker(engine, expire_on_commit=False, info={"file_store": store})
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
    return Services(
        settings=settings,
        engine=engine,
        sessions=sessions,
        jobs=RunService(sessions, event_bridge=bridge),
        knowledge=KnowledgeService(sessions),
        sources=SourceService(sessions, settings.source_storage_root),
        gateway=create_gateway(settings),
        runtime_redis=client,
        event_bridge=bridge,
        checkpoint_cache=cache,
    )
