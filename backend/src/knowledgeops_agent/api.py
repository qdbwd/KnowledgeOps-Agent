"""本机 Web 应用 HTTP 边界：短请求入队、持久结果读取、可信用户操作。

模型和工具不能调用内部授权方法。用户发布/编辑通过本模块进入同一知识
事务服务；SSE 只是事件传输，刷新、断线后都能从 PostgreSQL 重建页面。
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from knowledgeops_agent.config import get_settings
from knowledgeops_agent.event_stream import iter_run_events
from knowledgeops_agent.jobs import Budgets
from knowledgeops_agent.learning_api import learning_router
from knowledgeops_agent.mcp_calls import MCPCalls
from knowledgeops_agent.mcp_registry import MCPRegistry
from knowledgeops_agent.preferences import MemoryCategory, MemoryService, PreferenceService
from knowledgeops_agent.run_resources import ResourceService
from knowledgeops_agent.services import Services, create_services
from knowledgeops_agent.subagent_registry import SubagentCreate, SubagentRegistry, SubagentUpdate
from knowledgeops_agent.task_queue import OutboxPublisher, create_queue
from knowledgeops_agent.tool_catalog import catalog_view
from knowledgeops_agent.tool_policy import ToolPolicyService, ToolPolicyUpdate
from knowledgeops_agent.tool_preview import effective_catalog
from knowledgeops_agent.web_research import public_url

logger = logging.getLogger(__name__)


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ConversationInput(InputModel):
    title: str = Field(default="新对话", max_length=300)


class GuidanceInput(InputModel):
    target_root_id: str = Field(min_length=1, max_length=120)
    target_generation: int = Field(ge=1)


class PreferenceInput(InputModel):
    content: str = Field(min_length=1, max_length=2000)
    scope: Literal["global", "topic"] = "global"
    topic: str = Field(default="", max_length=200)


class PreferenceUpdate(InputModel):
    expected_revision: int = Field(ge=1)
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    scope: Literal["global", "topic"] | None = None
    topic: str | None = Field(default=None, max_length=200)
    status: Literal["active", "pending", "inactive", "deleted"] | None = None
    supersedes_id: str | None = Field(default=None, max_length=36)


class MemoryInput(PreferenceInput):
    category: MemoryCategory = "preference"


class MemoryUpdate(PreferenceUpdate):
    category: MemoryCategory | None = None


class MemoryRollback(InputModel):
    expected_revision: int = Field(ge=1)
    target_revision: int = Field(ge=1)


class MemoryMerge(InputModel):
    revisions: dict[str, int] = Field(min_length=2, max_length=10)
    content: str = Field(min_length=1, max_length=2000)


class MemoryExpectedRevision(InputModel):
    expected_revision: int = Field(ge=1)


class MessageInput(InputModel):
    content: str = Field(min_length=1, max_length=100000)
    request_key: str = Field(min_length=1, max_length=200)
    publication_mode: Literal["ask", "preview", "direct"] = "ask"
    knowledge_only: bool = False
    delegation: bool = True
    auxiliary: bool = Field(default=False, strict=True)
    source_ids: list[str] = Field(default_factory=list, max_length=30)
    budgets: Budgets = Field(default_factory=Budgets)
    learning_task_id: str | None = Field(default=None, min_length=1, max_length=36)


class MCPInput(InputModel):
    name: str = Field(min_length=1, max_length=200)
    config: dict


class MCPUpdate(InputModel):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict | None = None
    enabled: bool | None = None
    permissions: dict[str, Literal["deny", "read", "confirm"]] | None = None
    deleted: bool = False


class MCPDecision(InputModel):
    expected_hash: str = Field(min_length=1, max_length=64)
    approve: bool


class TextInput(InputModel):
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=2000000)


class UrlInput(InputModel):
    url: str = Field(min_length=1, max_length=4096)
    request_key: str = Field(min_length=1, max_length=160)
    refresh: bool = False


class RevisionInput(InputModel):
    expected_revision: int = Field(ge=1)


class PublishInput(RevisionInput):
    confirm_impact: bool = False


class DraftInput(RevisionInput):
    operations: list[dict] = Field(min_length=1, max_length=20)


class ResumeInput(InputModel):
    budgets: dict[str, int] = Field(default_factory=dict)


class EditInput(InputModel):
    request_key: str = Field(min_length=1, max_length=160)
    base_version_id: str
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=200000)
    summary: str = Field(default="", max_length=3000)
    category: str = Field(default="", max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=30)
    relations: list[dict[str, str]] = Field(default_factory=list, max_length=50)


class DocumentActionInput(InputModel):
    action: Literal["archive", "restore"]
    request_key: str = Field(min_length=1, max_length=160)
    base_version_id: str
    restore_version_id: str | None = None


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
            from knowledgeops_agent.workspace_storage import sync_knowledge_projection

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

    @app.exception_handler(ValueError)
    async def conflict(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(PermissionError)
    async def forbidden(request: Request, exc: PermissionError):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(IntegrityError)
    async def data_conflict(request: Request, exc: IntegrityError):
        # 不回传 SQL 参数和连接信息。保留冲突语义，客户端刷新后选择重试。
        return JSONResponse(
            {"detail": "data conflict; refresh the current version"}, status_code=409
        )

    def svc(request: Request) -> Services:
        return request.app.state.services

    @app.get("/api/health")
    def health(request: Request):
        with svc(request).engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/api/settings")
    def settings(request: Request):
        dependencies = svc(request)
        return {
            "model": dependencies.settings.agent_model,
            "model_calls_enabled": dependencies.gateway.enabled,
            "data_scope": "verification"
            if dependencies.engine.url.database == "knowledgeops_test"
            else "project",
        }

    @app.get("/api/conversations")
    def conversations(request: Request):
        return svc(request).jobs.conversations()

    @app.get("/api/tools/catalog")
    def tool_catalog(request: Request):
        # 统一展示复用已有MCP记录；查看目录不会连接服务或赋予执行权限。
        return catalog_view(MCPRegistry(svc(request).sessions).list())

    @app.get("/api/skills")
    def skills():
        from knowledgeops_agent.run_resources import ResourceUnavailable, skill_catalog

        try:
            return skill_catalog()
        except (ResourceUnavailable, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="技能配置不可用，请检查部署目录和文件格式") from exc

    @app.get("/api/tools/effective")
    def effective_tools(
        request: Request,
        role_id: str | None = None,
        knowledge_only: bool = False,
        auxiliary: bool = False,
        delegation: bool = True,
    ):
        return effective_catalog(
            svc(request).sessions,
            role_id=role_id,
            knowledge_only=knowledge_only,
            auxiliary=auxiliary,
            delegation=delegation,
            workspace_enabled=svc(request).settings.workspace_root is not None,
        )

    @app.get("/api/tools/policy")
    def tool_policy(request: Request):
        return ToolPolicyService(svc(request).sessions).get()

    @app.get("/api/tools/policy/history")
    def tool_policy_history(request: Request):
        return ToolPolicyService(svc(request).sessions).history()

    @app.patch("/api/tools/policy")
    def update_tool_policy(body: ToolPolicyUpdate, request: Request):
        return ToolPolicyService(svc(request).sessions).update(
            **body.model_dump(exclude_unset=True)
        )

    @app.get("/api/subagents")
    def subagents(request: Request):
        return SubagentRegistry(svc(request).sessions).list()

    @app.get("/api/subagents/options")
    def subagent_options():
        return SubagentRegistry.options()

    @app.post("/api/subagents", status_code=201)
    def create_subagent(body: SubagentCreate, request: Request):
        return SubagentRegistry(svc(request).sessions).create(**body.model_dump())

    @app.patch("/api/subagents/{definition_id}")
    def update_subagent(definition_id: str, body: SubagentUpdate, request: Request):
        return SubagentRegistry(svc(request).sessions).update(
            definition_id, **body.model_dump(exclude_unset=True)
        )

    @app.get("/api/subagents/{definition_id}/history")
    def subagent_history(definition_id: str, request: Request):
        return SubagentRegistry(svc(request).sessions).history(definition_id)

    @app.get("/api/mcp/connections")
    def mcp_connections(request: Request):
        return MCPRegistry(svc(request).sessions).list()

    @app.post("/api/mcp/connections", status_code=201)
    def create_mcp(body: MCPInput, request: Request):
        return MCPRegistry(svc(request).sessions).create(body.name, body.config)

    @app.patch("/api/mcp/connections/{connection_id}")
    def update_mcp(connection_id: str, body: MCPUpdate, request: Request):
        return MCPRegistry(svc(request).sessions).update(
            connection_id, **body.model_dump(exclude_unset=True)
        )

    @app.post("/api/mcp/connections/{connection_id}/discover")
    def discover_mcp(connection_id: str, body: RevisionInput, request: Request):
        return MCPRegistry(svc(request).sessions).discover(
            connection_id, expected_revision=body.expected_revision
        )

    @app.get("/api/mcp/connections/{connection_id}/export")
    def export_mcp(connection_id: str, request: Request):
        row = MCPRegistry(svc(request).sessions).get(connection_id)
        # 导入仍使用创建入口，默认禁用且需重新发现；导出不复制许可或服务凭据。
        return JSONResponse(
            {"name": row["name"], "config": row["config"]},
            headers={"Content-Disposition": 'attachment; filename="mcp-connection.json"'},
        )

    @app.get("/api/runs/{run_id}/mcp")
    def run_mcp_calls(run_id: str, request: Request):
        dependencies = svc(request)
        run = dependencies.jobs.get_run(run_id)
        return MCPCalls.for_root(dependencies.sessions, run["root_id"])

    @app.post("/api/runs/{run_id}/mcp/{receipt_id}/decision")
    def decide_mcp(run_id: str, receipt_id: str, body: MCPDecision, request: Request):
        from knowledgeops_agent.runtime import RunContext

        dependencies = svc(request)
        run = dependencies.jobs.get_run(run_id)
        context = RunContext(
            run_id,
            run["root_id"],
            "",
            dependencies.jobs,
            dependencies.knowledge,
            dependencies.sources,
            dependencies.gateway,
        )
        return MCPCalls(context).decide(
            receipt_id, expected_hash=body.expected_hash, approve=body.approve
        )

    @app.post("/api/conversations", status_code=201)
    def create_conversation(body: ConversationInput, request: Request):
        return svc(request).jobs.create_conversation(body.title)

    @app.get("/api/conversations/{conversation_id}/messages")
    def messages(conversation_id: str, request: Request):
        return svc(request).jobs.messages(conversation_id)

    @app.get("/api/conversations/{conversation_id}/context-usage")
    def conversation_context_usage(conversation_id: str, request: Request):
        from knowledgeops_agent.context_usage import context_usage

        dependencies = svc(request)
        return context_usage(dependencies.sessions, dependencies.gateway, conversation_id)

    @app.post("/api/conversations/{conversation_id}/messages", status_code=202)
    def send_message(conversation_id: str, body: MessageInput, request: Request):
        dependencies = svc(request)
        for source_id in body.source_ids:
            dependencies.sources.get_source(source_id)
        return dependencies.jobs.enqueue(
            conversation_id,
            body.content,
            request_key=body.request_key,
            publication_mode=body.publication_mode,
            knowledge_only=body.knowledge_only,
            delegation=body.delegation,
            auxiliary=body.auxiliary,
            source_ids=body.source_ids,
            budgets=body.budgets.model_dump(),
            learning_task_id=body.learning_task_id,
        )

    @app.get("/api/runs")
    def runs(request: Request, conversation_id: str | None = None, root_id: str | None = None):
        return svc(request).jobs.list_runs(conversation_id=conversation_id, root_id=root_id)

    @app.get("/api/runs/page")
    def runs_page(
        request: Request,
        conversation_id: str | None = None,
        root_id: str | None = None,
        status: str | None = None,
        roots_only: bool = False,
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=100),
    ):
        return svc(request).jobs.runs_page(
            conversation_id=conversation_id,
            root_id=root_id,
            status=status,
            roots_only=roots_only,
            cursor=cursor,
            limit=limit,
        )

    @app.get("/api/runs/{run_id}")
    def run(run_id: str, request: Request):
        dependencies = svc(request)
        result = dependencies.jobs.get_run(run_id)
        result["model_usage"] = dependencies.gateway.ledger.summary(result["root_id"])
        return result

    @app.get("/api/runs/{run_id}/records")
    def run_records(run_id: str, request: Request, after: Annotated[int, Query(ge=0)] = 0):
        dependencies = svc(request)
        row = dependencies.jobs.get_run(run_id)
        # 每次读取固定到已观察的事件序号；持续流式写入不会让当前页边界漂移。
        events = [
            event
            for event in dependencies.jobs.events(run_id, after=after, limit=200)
            if event["sequence"] <= row["last_event"]
        ]
        cursor = events[-1]["sequence"] if events else after
        return {
            "run": row,
            "models": dependencies.gateway.ledger.records(root_id=row["root_id"]),
            "events": events,
            "event_page": {
                "after": after,
                "next_after": cursor if cursor < row["last_event"] else None,
                "total": row["last_event"],
            },
            "artifacts": dependencies.jobs.artifacts(run_id),
            "resources": ResourceService(dependencies.sessions).read(row["root_id"]),
            "continuation": dependencies.jobs.continuation(run_id),
            "guidance": dependencies.jobs.guidance(row["root_id"]),
            "mcp_calls": MCPCalls.for_root(dependencies.sessions, row["root_id"]),
        }

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id}")
    def run_artifact(run_id: str, artifact_id: str, request: Request, start: int = 0):
        jobs = svc(request).jobs
        return jobs.read_artifact(artifact_id, root_id=jobs.get_run(run_id)["root_id"], start=start)

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = 0):
        dependencies = svc(request)
        await asyncio.to_thread(dependencies.jobs.get_run, run_id)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(422, "invalid event cursor") from None

        return StreamingResponse(
            iter_run_events(
                dependencies.jobs,
                dependencies.event_bridge,
                run_id,
                cursor,
                request.is_disconnected,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/preferences")
    def preferences(request: Request, include_deleted: bool = False):
        return PreferenceService(svc(request).sessions).list(include_deleted=include_deleted)

    @app.post("/api/preferences", status_code=201)
    def create_preference(body: PreferenceInput, request: Request):
        return PreferenceService(svc(request).sessions).create(**body.model_dump())

    @app.patch("/api/preferences/{preference_id}")
    def edit_preference(preference_id: str, body: PreferenceUpdate, request: Request):
        return PreferenceService(svc(request).sessions).update(
            preference_id, **body.model_dump(exclude_unset=True)
        )

    @app.get("/api/preferences/{preference_id}/history")
    def preference_history(preference_id: str, request: Request):
        return PreferenceService(svc(request).sessions).history(preference_id)

    @app.get("/api/memories")
    def memories(request: Request, include_deleted: bool = False):
        return MemoryService(svc(request).sessions).list(include_deleted=include_deleted)

    @app.get("/api/memories/search")
    def search_memories(
        request: Request,
        query: str = Query(min_length=1, max_length=500),
        topic: str = Query(default="", max_length=200),
        limit: int = Query(default=8, ge=1, le=50),
    ):
        return MemoryService(svc(request).sessions).search(query, topic=topic, limit=limit)

    @app.post("/api/memories", status_code=201)
    def create_memory(body: MemoryInput, request: Request):
        return MemoryService(svc(request).sessions).create(**body.model_dump())

    @app.post("/api/memories/merge", status_code=201)
    def merge_memories(body: MemoryMerge, request: Request):
        return MemoryService(svc(request).sessions).merge(**body.model_dump())

    @app.get("/api/memories/{memory_id}")
    def memory_detail(memory_id: str, request: Request):
        return MemoryService(svc(request).sessions).get(memory_id)

    @app.patch("/api/memories/{memory_id}")
    def edit_memory(memory_id: str, body: MemoryUpdate, request: Request):
        return MemoryService(svc(request).sessions).update(
            memory_id, **body.model_dump(exclude_unset=True)
        )

    @app.get("/api/memories/{memory_id}/history")
    def memory_history(memory_id: str, request: Request):
        return MemoryService(svc(request).sessions).history(memory_id)

    @app.post("/api/memories/{memory_id}/rollback")
    def rollback_memory(memory_id: str, body: MemoryRollback, request: Request):
        return MemoryService(svc(request).sessions).rollback(memory_id, **body.model_dump())

    @app.post("/api/memories/{memory_id}/undo-merge")
    def undo_memory_merge(memory_id: str, body: MemoryExpectedRevision, request: Request):
        return MemoryService(svc(request).sessions).undo_merge(memory_id, **body.model_dump())

    @app.get("/api/runs/{run_id}/guidance")
    def guidance(run_id: str, request: Request):
        jobs = svc(request).jobs
        return jobs.guidance(jobs.get_run(run_id)["root_id"])

    @app.post("/api/runs/{run_id}/guide")
    def guide(run_id: str, body: GuidanceInput, request: Request):
        return svc(request).jobs.guide(
            run_id, target_id=body.target_root_id, target_generation=body.target_generation
        )

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str, request: Request):
        return svc(request).jobs.cancel(run_id)

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    def resume(run_id: str, body: ResumeInput, request: Request):
        dependencies = svc(request)
        row = dependencies.jobs.get_run(run_id)
        if row["parent_id"] or row["status"] not in {
            "failed",
            "cancelled",
            "interrupted",
            "budget_exhausted",
        }:
            raise ValueError("only stopped root tasks can be resumed")
        limits = Budgets.model_validate({**row["budgets"], **body.budgets})
        usage = dependencies.gateway.ledger.summary(row["root_id"])
        if row["kind"] == "material":
            # 确定性材料解析不消耗模型预算。
            return dependencies.jobs.resume(run_id, budgets=limits.model_dump())
        if usage["root_used"] >= limits.model_requests:
            raise ValueError("remaining model request budget required before resume")
        # 先同步账本授权再使任务可投递；任务仍用自身预算取较小上限。
        dependencies.gateway.ledger.set_root_limit(row["root_id"], limits.model_requests)
        return dependencies.jobs.resume(run_id, budgets=limits.model_dump())

    @app.get("/api/sources")
    def sources(request: Request):
        return svc(request).sources.list_sources()

    @app.post("/api/sources/text", status_code=201)
    def source_text(body: TextInput, request: Request):
        return svc(request).sources.import_text(body.title, body.content)

    @app.post("/api/sources/url", status_code=202)
    def source_url(body: UrlInput, request: Request):
        return svc(request).jobs.enqueue_material(
            {"type": "web", "url": str(public_url(body.url)), "refresh": body.refresh},
            request_key=body.request_key,
        )

    @app.post("/api/sources/upload", status_code=201)
    async def upload(request: Request, file: Annotated[UploadFile, File()]):
        data = await file.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(413, "maximum upload size is 10 MiB")
        title = Path(file.filename or "材料.txt").name[:500]
        if Path(title).suffix.lower() == ".pdf":
            source = await asyncio.to_thread(svc(request).sources.register_pdf, title, data)
            run = await asyncio.to_thread(
                svc(request).jobs.enqueue_material,
                {"type": "pdf", "version_id": source["version_id"]},
                request_key="pdf:" + source["version_id"],
            )
            return JSONResponse({"source_id": source["id"], "job_id": run["id"]}, status_code=202)
        if Path(title).suffix.lower() not in {".md", ".txt"}:
            raise HTTPException(422, "supported uploads: Markdown, TXT and PDF")
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(422, "text file must use UTF-8 encoding") from None
        return await asyncio.to_thread(
            svc(request).sources.import_text, title, content, kind="file", raw_bytes=data
        )

    @app.get("/api/workspace/download")
    def workspace_download(request: Request, path: str = Query(max_length=2000)):
        """只传输挂载区交付文件，不执行文件、不开放宿主任意路径。"""
        from knowledgeops_agent.workspace_tools import workspace_file

        root = svc(request).settings.workspace_root
        if root is None:
            raise HTTPException(404, "workspace is not configured")
        target = workspace_file(root, path, delivery=True)
        return FileResponse(
            target,
            filename=target.name,
            media_type="application/octet-stream",
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    @app.get("/api/sources/{source_id}")
    def source(source_id: str, request: Request, version_id: str | None = None):
        return svc(request).sources.get_source(source_id, version_id=version_id)

    @app.get("/api/source-versions/{version_id}/original")
    def source_original(version_id: str, request: Request):
        data, mime = svc(request).sources.original(version_id)
        return Response(
            data,
            media_type=mime,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": "attachment"
                if mime in {"text/html", "application/xhtml+xml"}
                else "inline",
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    @app.get("/api/source-segments/{segment_id}")
    def source_segment(segment_id: str, request: Request):
        return svc(request).sources.get_segment(segment_id)

    @app.get("/api/evidence/{evidence_id}")
    def evidence(evidence_id: str, request: Request):
        return svc(request).sources.get_evidence(evidence_id)

    @app.get("/api/knowledge")
    def knowledge(request: Request, query: str = "", include_archived: bool = False):
        service = svc(request).knowledge
        return (
            service.search(query)
            if query.strip()
            else service.list_documents(include_archived=include_archived)
        )

    @app.get("/api/knowledge/export")
    def export(request: Request):
        data = svc(request).knowledge.export_markdown()
        return Response(
            data,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="knowledgeops-markdown.zip"',
            },
        )

    @app.get("/api/knowledge/{document_id}")
    def document(document_id: str, request: Request, version_id: str | None = None):
        return svc(request).knowledge.get_document(document_id, version_id=version_id)

    @app.get("/api/knowledge/{document_id}/versions")
    def versions(document_id: str, request: Request):
        return svc(request).knowledge.history(document_id)

    @app.post("/api/knowledge/{document_id}/edits")
    def edit_document(document_id: str, body: EditInput, request: Request):
        service = svc(request).knowledge
        root_id = "user-edit:" + body.request_key
        operation = {
            **body.model_dump(exclude={"request_key"}, exclude_unset=True),
            "action": "update",
            "document_id": document_id,
        }
        draft = service.create_draft(root_id, [operation], operation_key=root_id)
        if draft["status"] != "published":
            service.approve(draft["id"], expected_revision=draft["revision"])
        return service.publish(
            draft["id"], idempotency_key=root_id, expected_revision=draft["revision"]
        )

    @app.post("/api/knowledge/{document_id}/actions", status_code=201)
    def document_action(document_id: str, body: DocumentActionInput, request: Request):
        """页面归档/恢复先生成可审阅变更，与 Agent 和编辑共用版本事务。"""
        root_id = "user-action:" + body.request_key
        return svc(request).knowledge.create_draft(
            root_id,
            [{**body.model_dump(exclude={"request_key"}), "document_id": document_id}],
            operation_key=root_id,
        )

    @app.get("/api/changesets")
    def changesets(request: Request, root_id: str | None = None):
        return svc(request).knowledge.list_drafts(root_id=root_id)

    @app.get("/api/changesets/page")
    def changesets_page(
        request: Request,
        root_id: str | None = None,
        conversation_id: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=100),
    ):
        return svc(request).knowledge.drafts_page(
            root_id=root_id,
            conversation_id=conversation_id,
            status=status,
            cursor=cursor,
            limit=limit,
        )

    @app.get("/api/changesets/{changeset_id}")
    def changeset(changeset_id: str, request: Request):
        return svc(request).knowledge.get_draft(changeset_id)

    @app.put("/api/changesets/{changeset_id}")
    def edit_changeset(changeset_id: str, body: DraftInput, request: Request):
        return svc(request).knowledge.edit_draft(
            changeset_id, body.operations, expected_revision=body.expected_revision
        )

    @app.post("/api/changesets/{changeset_id}/reject")
    def reject(changeset_id: str, body: RevisionInput, request: Request):
        return svc(request).knowledge.reject(changeset_id, expected_revision=body.expected_revision)

    @app.post("/api/changesets/{changeset_id}/publish")
    def publish(changeset_id: str, body: PublishInput, request: Request):
        service = svc(request).knowledge
        draft = service.get_draft(changeset_id)
        if draft["status"] != "published":
            service.approve(
                changeset_id,
                confirm_impact=body.confirm_impact,
                expected_revision=body.expected_revision,
            )
        return service.publish(
            changeset_id,
            idempotency_key=f"publish:{changeset_id}",
            expected_revision=body.expected_revision,
        )

    @app.get("/api/runs/{run_id}/export")
    def run_export(run_id: str, request: Request):
        records = run_records(run_id, request)
        target = records["run"]["last_event"]
        cursor = records["events"][-1]["sequence"] if records["events"] else 0
        # 导出覆盖快照中的所有页。运行仍活跃时只导出 target 之前的事件，
        # 不无限追赶新流；目标序号保留在 run.last_event/event_page.total 中。
        while cursor < target:
            batch = [
                event
                for event in svc(request).jobs.events(run_id, after=cursor, limit=1000)
                if event["sequence"] <= target
            ]
            if not batch:
                raise RuntimeError("run event snapshot is incomplete")
            records["events"].extend(batch)
            cursor = batch[-1]["sequence"]
        records["event_page"]["next_after"] = None
        return Response(
            json.dumps(jsonable_encoder(records), ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="run-{run_id}.json"',
            },
        )

    return app


app = create_app()
