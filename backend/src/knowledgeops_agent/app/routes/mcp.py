"""mcp HTTP 接口；业务处理委托给已有应用服务。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from knowledgeops_agent.app.dependencies import svc
from knowledgeops_agent.app.schemas import (
    MCPActivationInput,
    MCPCredentialsInput,
    MCPDecision,
    MCPImportInput,
    MCPInput,
    MCPUpdate,
    RevisionInput,
)
from knowledgeops_agent.harness.mcp.calls import MCPCalls
from knowledgeops_agent.harness.mcp.lifecycle import MCPCleaner
from knowledgeops_agent.harness.mcp.management import MCPManagement
from knowledgeops_agent.harness.mcp.registry import MCPRegistry

router = APIRouter()


@router.get('/api/mcp/cleanup')
def mcp_cleanup(request: Request):
    return MCPCleaner(MCPManagement(svc(request).sessions)).list()


@router.post('/api/mcp/cleanup/{operation_id}/retry', status_code=202)
def retry_mcp_cleanup(operation_id: str, request: Request):
    MCPCleaner(MCPManagement(svc(request).sessions)).retry(operation_id)
    return {'status': 'queued'}


def _external_config(config):
    """旧接口只兼容外部入口；受管安装只能通过已测试的候选激活，防止绕过回收状态。"""
    from knowledgeops_agent.harness.mcp.installations import MCPInstallStorage

    if config:
        if config.get('installation_id'):
            raise ValueError('应用管理的MCP安装请通过候选准备、测试和激活')
        MCPInstallStorage().reject_external_references(config)


@router.post('/api/mcp/import-preview')
def preview_mcp(body: MCPImportInput):
    return MCPManagement.preview(body.value)


@router.post('/api/mcp/import', status_code=201)
def import_mcp(body: MCPImportInput, request: Request):
    return MCPManagement(svc(request).sessions).import_config(**body.model_dump())


@router.get('/api/mcp/operations')
def mcp_operations(request: Request):
    return MCPManagement(svc(request).sessions).list()


@router.get('/api/mcp/operations/{operation_id}')
def mcp_operation(operation_id: str, request: Request):
    return MCPManagement(svc(request).sessions).get(operation_id)


@router.post('/api/mcp/operations/{operation_id}/prepare', status_code=202)
def prepare_mcp(operation_id: str, request: Request):
    return MCPManagement(svc(request).sessions).prepare(operation_id)


@router.post('/api/mcp/operations/{operation_id}/test')
def test_mcp_candidate(operation_id: str, request: Request):
    return MCPManagement(svc(request).sessions).test(operation_id)


@router.post('/api/mcp/operations/{operation_id}/cancel')
def cancel_mcp_candidate(operation_id: str, request: Request):
    return MCPManagement(svc(request).sessions).cancel(operation_id)


@router.delete('/api/mcp/operations/{operation_id}')
def remove_mcp_candidate(operation_id: str, request: Request):
    return MCPManagement(svc(request).sessions).discard(operation_id)


@router.post('/api/mcp/operations/{operation_id}/activate')
def activate_mcp_candidate(operation_id: str, body: MCPActivationInput, request: Request):
    return MCPManagement(svc(request).sessions).activate(operation_id, **body.model_dump())


@router.get('/api/mcp/operations/{operation_id}/credentials')
def candidate_credentials(operation_id: str, request: Request):
    return MCPManagement(svc(request).sessions).credentials(operation_id)


@router.put('/api/mcp/operations/{operation_id}/credentials')
def set_candidate_credentials(operation_id: str, body: MCPCredentialsInput, request: Request):
    return MCPManagement(svc(request).sessions).credentials(operation_id, values=body.values)


@router.get('/api/mcp/connections/{connection_id}/credentials')
def connection_credentials(connection_id: str, request: Request):
    return MCPManagement(svc(request).sessions).credentials(connection_id, connection=True)


@router.get('/api/mcp/connections/{connection_id}/removal-preview')
def mcp_removal_preview(connection_id: str, request: Request):
    return MCPManagement(svc(request).sessions).removal_preview(connection_id)


@router.put('/api/mcp/connections/{connection_id}/credentials')
def set_connection_credentials(connection_id: str, body: MCPCredentialsInput, request: Request):
    return MCPManagement(svc(request).sessions).credentials(connection_id, connection=True, **body.model_dump())


@router.get("/api/mcp/connections")
def mcp_connections(request: Request):
    return MCPRegistry(svc(request).sessions).list()


@router.post("/api/mcp/connections", status_code=201)
def create_mcp(body: MCPInput, request: Request):
    _external_config(body.config)
    return MCPRegistry(svc(request).sessions).create(body.name, body.config)


@router.patch("/api/mcp/connections/{connection_id}")
def update_mcp(connection_id: str, body: MCPUpdate, request: Request):
    _external_config(body.config)
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
    # 托管入口导出包描述，跨设备重新明确准备；不复制机器绝对安装路径和许可。
    return JSONResponse(
        MCPManagement(svc(request).sessions).export_connection(connection_id),
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
