"""本机 Web 应用 HTTP 边界：短请求入队、持久结果读取、可信用户操作。

模型和工具不能调用内部授权方法。用户发布/编辑通过本模块进入同一知识
事务服务；SSE 只是事件传输，刷新、断线后都能从 PostgreSQL 重建页面。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from knowledgeops_agent.app.dependencies import svc
from knowledgeops_agent.app.routes.assets import router as assets_router
from knowledgeops_agent.app.routes.changesets import router as changesets_router
from knowledgeops_agent.app.routes.conversations import router as conversations_router
from knowledgeops_agent.app.routes.execution import execution_router
from knowledgeops_agent.app.routes.knowledge import router as knowledge_router
from knowledgeops_agent.app.routes.learning import learning_router
from knowledgeops_agent.app.routes.mcp import router as mcp_router
from knowledgeops_agent.app.routes.memory import router as memory_router
from knowledgeops_agent.app.routes.runs import router as runs_router
from knowledgeops_agent.app.routes.sources import router as sources_router
from knowledgeops_agent.app.routes.subagents import router as subagents_router
from knowledgeops_agent.app.routes.system import router as system_router
from knowledgeops_agent.app.routes.tools import router as tools_router
from knowledgeops_agent.app.routes.workspace import router as workspace_router
from knowledgeops_agent.app.runs.queue import OutboxPublisher, create_queue
from knowledgeops_agent.app.services import Services, create_services
from knowledgeops_agent.core.config import get_settings

logger = logging.getLogger(__name__)


def create_app(*, services: Services | None = None, publish_jobs: bool = True) -> FastAPI:
    """应用工厂支持隔离测试注入；导入模块不连接数据库或请求模型。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned = services is None
        dependencies = services or create_services(get_settings())
        app.state.services = dependencies
        if dependencies.settings.workspace_root and dependencies.sessions.kw.get("info", {}).get(
            "file_store"
        ):
            from knowledgeops_agent.persistence.workspace import sync_knowledge_projection

            try:
                await asyncio.to_thread(sync_knowledge_projection, dependencies.sessions)
            except (OSError, ValueError) as error:
                logger.warning("knowledge file view needs repair (%s)", type(error).__name__)
        stop = asyncio.Event()

        async def publish_loop():
            publisher = OutboxPublisher(
                dependencies.sessions, create_queue(dependencies.settings.celery_broker_url)
            )
            while not stop.is_set():
                try:
                    await asyncio.to_thread(publisher.publish_due)
                except Exception as exc:
                    logger.warning("job publisher unavailable (%s)", type(exc).__name__)
                try:
                    result = await asyncio.to_thread(dependencies.lifecycle.tick)
                    if (result.get("scan") or {}).get("error"):
                        logger.warning("lifecycle scan needs retry (%s)", result["scan"]["error"])
                except Exception as exc:
                    logger.warning("lifecycle maintenance unavailable (%s)", type(exc).__name__)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1)
                except TimeoutError:
                    pass

        task = asyncio.create_task(publish_loop()) if publish_jobs else None
        try:
            yield
        finally:
            stop.set()
            if task is not None:
                await task
            if owned:
                dependencies.close()

    app = FastAPI(title="KnowledgeOps", version="0.1.0", lifespan=lifespan)
    app.include_router(learning_router())
    app.include_router(execution_router)

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        # 本机单用户不是任意Host都可信。先拒绝DNS重绑定，再校验浏览器来源。
        host = request.headers.get("host", "")
        allowed = set(svc(request).settings.api_allowed_hosts.split(","))
        if services is not None:
            # 仅显式注入隔离测试服务的应用可使用TestClient默认主机。
            allowed.add("testserver")
        try:
            target = urlsplit("http://" + host)
            valid = (
                target.hostname in allowed
                and not target.username
                and not target.password
                and not target.path
                and not target.query
                and not target.fragment
            )
            _ = target.port  # 无效端口必须变成400，而不是在后续URL处理中抛500。
        except ValueError:
            valid = False
        if not valid:
            return JSONResponse({"detail": "untrusted request host"}, status_code=400)
        origin = request.headers.get("origin")
        if origin:
            try:
                parsed = urlsplit(origin)
                _ = parsed.port
            except ValueError:
                return JSONResponse(
                    {"detail": "cross-origin request is not allowed"}, status_code=403
                )
            if (
                parsed.netloc.lower() != host.lower()
                or parsed.scheme != request.url.scheme
                or parsed.path not in {"", "/"}
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                return JSONResponse(
                    {"detail": "cross-origin request is not allowed"}, status_code=403
                )
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "cross-site request is not allowed"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(KeyError)
    async def missing(request: Request, exc: KeyError):
        return JSONResponse({"detail": str(exc).strip("'")}, status_code=404)

    from knowledgeops_agent.app.lifecycle.access import DetailsExpired

    @app.exception_handler(DetailsExpired)
    async def expired_details(request: Request, exc: DetailsExpired):
        return JSONResponse({"detail": str(exc), "code": "details_expired"}, status_code=410)

    @app.exception_handler(ValueError)
    async def conflict(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    from knowledgeops_agent.harness.tools.configuration import GroupSelectionError

    @app.exception_handler(GroupSelectionError)
    async def invalid_group_selection(request: Request, exc: GroupSelectionError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(PermissionError)
    async def forbidden(request: Request, exc: PermissionError):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(IntegrityError)
    async def data_conflict(request: Request, exc: IntegrityError):
        # 不回传 SQL 参数和连接信息。保留冲突语义，客户端刷新后选择重试。
        return JSONResponse(
            {"detail": "data conflict; refresh the current version"}, status_code=409
        )

    app.include_router(system_router)
    app.include_router(conversations_router)
    app.include_router(tools_router)
    app.include_router(subagents_router)
    app.include_router(mcp_router)
    app.include_router(runs_router)
    app.include_router(memory_router)
    app.include_router(sources_router)
    app.include_router(assets_router)
    app.include_router(workspace_router)
    app.include_router(knowledge_router)
    app.include_router(changesets_router)
    return app
