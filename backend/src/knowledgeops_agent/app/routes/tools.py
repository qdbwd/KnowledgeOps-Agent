"""tools HTTP 接口；业务处理委托给已有应用服务。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from knowledgeops_agent.app.dependencies import svc
from knowledgeops_agent.harness.mcp.registry import MCPRegistry
from knowledgeops_agent.harness.tools.catalog import catalog_view
from knowledgeops_agent.harness.tools.policy import ToolPolicyService, ToolPolicyUpdate
from knowledgeops_agent.harness.tools.preview import effective_catalog

router = APIRouter()


@router.get("/api/tools/catalog")
def tool_catalog(request: Request):
    # 统一展示复用已有MCP记录；查看目录不会连接服务或赋予执行权限。
    return catalog_view(MCPRegistry(svc(request).sessions).list())


@router.get("/api/skills")
def skills(request: Request):
    from knowledgeops_agent.harness.runtime.resources import ResourceUnavailable, skill_catalog

    try:
        policy = ToolPolicyService(svc(request).sessions).get()
        return [
            {
                **item,
                "available": item["enabled"],
                "enabled": item["enabled"]
                and policy.get("skill_enabled", {}).get(item["name"], True),
                "policy_revision": policy["revision"],
            }
            for item in skill_catalog()
        ]
    except (ResourceUnavailable, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503, detail="技能配置不可用，请检查部署目录和文件格式"
        ) from exc


@router.get("/api/tools/effective")
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


@router.get("/api/tools/policy")
def tool_policy(request: Request):
    return ToolPolicyService(svc(request).sessions).get()


@router.get("/api/tools/policy/history")
def tool_policy_history(request: Request):
    return ToolPolicyService(svc(request).sessions).history()


@router.patch("/api/tools/policy")
def update_tool_policy(body: ToolPolicyUpdate, request: Request):
    return ToolPolicyService(svc(request).sessions).update(**body.model_dump(exclude_unset=True))
