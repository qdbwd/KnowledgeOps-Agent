"""本机 Web 应用 HTTP 边界：短请求入队、持久结果读取、可信用户操作。

模型和工具不能调用内部授权方法。用户发布/编辑通过本模块进入同一知识
事务服务；SSE 只是事件传输，刷新、断线后都能从 PostgreSQL 重建页面。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from knowledgeops_agent.app.dependencies import svc
from knowledgeops_agent.app.mcp_operations import MCPPreparationPublisher
from knowledgeops_agent.app.routes.activity import router as activity_router
from knowledgeops_agent.app.routes.assets import router as assets_router
from knowledgeops_agent.app.routes.changesets import router as changesets_router
from knowledgeops_agent.app.routes.conversations import router as conversations_router
from knowledgeops_agent.app.routes.execution import execution_router
from knowledgeops_agent.app.routes.knowledge import router as knowledge_router
from knowledgeops_agent.app.routes.learning import learning_router
from knowledgeops_agent.app.routes.mcp import router as mcp_router
from knowledgeops_agent.app.routes.memory import router as memory_router
from knowledgeops_agent.app.routes.runs import router as runs_router
from knowledgeops_agent.app.routes.skills import router as skills_router
from knowledgeops_agent.app.routes.sources import router as sources_router
from knowledgeops_agent.app.routes.subagents import router as subagents_router
from knowledgeops_agent.app.routes.system import router as system_router
from knowledgeops_agent.app.routes.tools import router as tools_router
from knowledgeops_agent.app.routes.workspace import router as workspace_router
from knowledgeops_agent.app.runs.queue import OutboxPublisher, create_queue
from knowledgeops_agent.app.services import Services, create_services
from knowledgeops_agent.core.config import get_settings
from knowledgeops_agent.persistence.maintenance import MaintenanceLease, MaintenanceLost

logger = logging.getLogger(__name__)


def create_app(*, services: Services | None = None, publish_jobs: bool = True) -> FastAPI:
    """应用工厂支持隔离测试注入；导入模块不连接数据库或请求模型。"""

    @asynccontextmanager
    async def application_lifespan(app: FastAPI, dependencies: Services):
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
            queue = create_queue(dependencies.settings.celery_broker_url)
            publisher = OutboxPublisher(dependencies.sessions, queue)
            mcp_publisher = MCPPreparationPublisher(dependencies.sessions, queue)
            while not stop.is_set():
                try:
                    await asyncio.to_thread(dependencies.check_maintenance)
                except MaintenanceLost:
                    logger.error("maintenance protection lost; background writes stopped")
                    return
                try:
                    await asyncio.to_thread(publisher.publish_due)
                except Exception as exc:
                    logger.warning("job publisher unavailable (%s)", type(exc).__name__)
                try:
                    await asyncio.to_thread(mcp_publisher.publish_due)
                except Exception as exc:
                    logger.warning("MCP preparation publisher unavailable (%s)", type(exc).__name__)
                try:
                    result = await asyncio.to_thread(dependencies.lifecycle.tick)
                    if (result.get("scan") or {}).get("error"):
                        logger.warning("lifecycle scan needs retry (%s)", result["scan"]["error"])
                    if (result.get("skills") or {}).get("error"):
                        logger.warning("skill operation recovery needs retry (%s)", result["skills"]["error"])
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned = services is None
        dependencies = services or create_services(get_settings())
        # 注入测试服务也保护启动时的文件投影；异常启动必须释放锁与依赖。
        temporary_guard = None
        try:
            if dependencies.maintenance is None:
                temporary_guard = MaintenanceLease(
                    dependencies.engine, workspace=dependencies.settings.workspace_root
                ).acquire()
                dependencies.maintenance = temporary_guard
            async with application_lifespan(app, dependencies):
                yield
        finally:
            if owned:
                dependencies.close()
            elif temporary_guard is not None:
                temporary_guard.close()
                dependencies.maintenance = None

    app = FastAPI(title="KnowledgeOps", version="0.1.0", lifespan=lifespan)

    def failure(request: Request, status: int, detail: str, code: str, action: str) -> JSONResponse:
        """统一错误分类：稳定 code、可引用的 request_id 和恢复动作。

        只增加分类字段，不改变各业务原本的 detail、状态码或是否允许重试；
        客户端据此说明"发生了什么、可以做什么"，未知副作用仍走原人工恢复。
        """
        return JSONResponse(
            {
                "detail": detail,
                "code": code,
                "action": action,
                "request_id": getattr(request.state, "request_id", ""),
            },
            status_code=status,
        )

    app.include_router(learning_router())
    app.include_router(execution_router)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # 新MCP导入/密钥表单会短暂携带原值，验证响应不能回显input/context。
        if request.url.path.startswith('/api/mcp/'):
            return failure(request, 422, 'MCP请求格式无效，请检查字段和参数类型',
                           'invalid_request', '按提示修正字段后重新提交')
        return await request_validation_exception_handler(request, error)

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        # 每个请求一个可引用的编号：错误响应与响应头一致，便于对照日志。
        request.state.request_id = uuid4().hex[:12]
        try:
            await asyncio.to_thread(svc(request).check_maintenance)
        except MaintenanceLost:
            return failure(request, 503, "维护保护已失效，请停止应用并检查数据库连接后重新启动",
                           "maintenance_lost", "停止应用并检查数据库连接后重新启动")
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
            return failure(request, 400, "untrusted request host", "untrusted_host",
                           "用允许的宿主地址打开应用后再试")
        origin = request.headers.get("origin")
        if origin:
            try:
                parsed = urlsplit(origin)
                _ = parsed.port
            except ValueError:
                return failure(request, 403, "cross-origin request is not allowed",
                               "cross_origin", "从应用自身的页面发起请求")
            if (
                parsed.netloc.lower() != host.lower()
                or parsed.scheme != request.url.scheme
                or parsed.path not in {"", "/"}
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                return failure(request, 403, "cross-origin request is not allowed",
                               "cross_origin", "从应用自身的页面发起请求")
        if request.headers.get("sec-fetch-site") == "cross-site":
            return failure(request, 403, "cross-site request is not allowed",
                           "cross_origin", "从应用自身的页面发起请求")
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(KeyError)
    async def missing(request: Request, exc: KeyError):
        return failure(request, 404, str(exc).strip("'"), "not_found",
                       "刷新列表或确认身份后再试")

    from knowledgeops_agent.app.lifecycle.access import DetailsExpired

    @app.exception_handler(DetailsExpired)
    async def expired_details(request: Request, exc: DetailsExpired):
        return failure(request, 410, str(exc), "details_expired",
                       "打开独立交付或重新生成结果")

    @app.exception_handler(ValueError)
    async def conflict(request: Request, exc: ValueError):
        return failure(request, 409, str(exc), "conflict", "刷新后核对最新版本")

    from knowledgeops_agent.app.conversations import CursorStale

    @app.exception_handler(CursorStale)
    async def stale_cursor(request: Request, exc: CursorStale):
        return failure(request, 409, str(exc), "cursor_stale", "刷新列表后重新定位")

    from knowledgeops_agent.harness.tools.configuration import GroupSelectionError

    @app.exception_handler(GroupSelectionError)
    async def invalid_group_selection(request: Request, exc: GroupSelectionError):
        return failure(request, 422, str(exc), "invalid_request", "按提示修正选择后重试")

    @app.exception_handler(PermissionError)
    async def forbidden(request: Request, exc: PermissionError):
        return failure(request, 403, str(exc), "forbidden", "检查权限或改由人工执行")

    @app.exception_handler(IntegrityError)
    async def data_conflict(request: Request, exc: IntegrityError):
        # 不回传 SQL 参数和连接信息。保留冲突语义，客户端刷新后选择重试。
        return failure(request, 409, "data conflict; refresh the current version",
                       "data_conflict", "刷新后重试")

    app.include_router(system_router)
    app.include_router(activity_router)
    app.include_router(conversations_router)
    app.include_router(tools_router)
    app.include_router(skills_router)
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
