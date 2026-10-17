"""mcp HTTP 接口；业务处理委托给已有应用服务。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from knowledgeops_agent.app.dependencies import svc
from knowledgeops_agent.app.schemas import (
    MCPDecision,
    MCPInput,
    MCPUpdate,
    RevisionInput,
)
from knowledgeops_agent.harness.mcp.calls import MCPCalls
from knowledgeops_agent.harness.mcp.registry import MCPRegistry

router = APIRouter()


@router.get("/api/mcp/connections")
def mcp_connections(request: Request):
    return MCPRegistry(svc(request).sessions).list()


@router.post("/api/mcp/connections", status_code=201)
def create_mcp(body: MCPInput, request: Request):
    return MCPRegistry(svc(request).sessions).create(body.name, body.config)


@router.patch("/api/mcp/connections/{connection_id}")
def update_mcp(connection_id: str, body: MCPUpdate, request: Request):
    return MCPRegistry(svc(request).sessions).update(
        connection_id, **body.model_dump(exclude_unset=True)
    )


@router.post("/api/mcp/connections/{connection_id}/discover")
def discover_mcp(connection_id: str, body: RevisionInput, request: Request):
    return MCPRegistry(svc(request).sessions).discover(
        connection_id, expected_revision=body.expected_revision
    )


@router.get("/api/mcp/connections/{connection_id}/export")
def export_mcp(connection_id: str, request: Request):
    row = MCPRegistry(svc(request).sessions).get(connection_id)
    # 导入仍使用创建入口，默认禁用且需重新发现；导出不复制许可或服务凭据。
    return JSONResponse(
        {"name": row["name"], "config": row["config"]},
        headers={"Content-Disposition": 'attachment; filename="mcp-connection.json"'},
    )


@router.get("/api/runs/{run_id}/mcp")
def run_mcp_calls(run_id: str, request: Request):
    dependencies = svc(request)
    run = dependencies.jobs.get_run(run_id)
    return MCPCalls.for_root(dependencies.sessions, run["root_id"])


@router.post("/api/runs/{run_id}/mcp/{receipt_id}/decision")
def decide_mcp(run_id: str, receipt_id: str, body: MCPDecision, request: Request):
    from knowledgeops_agent.harness.runtime.agent import RunContext

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
