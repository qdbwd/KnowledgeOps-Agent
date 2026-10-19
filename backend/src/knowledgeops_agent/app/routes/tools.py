"""tools HTTP 接口；业务处理委托给已有应用服务。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from knowledgeops_agent.app.dependencies import svc
from knowledgeops_agent.harness.mcp.registry import MCPRegistry
from knowledgeops_agent.harness.skills.presentation import present_skill
from knowledgeops_agent.harness.tools.catalog import catalog_view
from knowledgeops_agent.harness.tools.groups import (
    GroupDelete,
    GroupEdit,
    GroupFields,
    GroupUpdate,
    ToolGroupService,
    read_groups,
)
from knowledgeops_agent.harness.tools.policy import ToolPolicyService, ToolPolicyUpdate
from knowledgeops_agent.harness.tools.preview import effective_catalog

router = APIRouter()


@router.get("/api/tools/detail")
def tool_details(tool_id: str, request: Request):
    from knowledgeops_agent.harness.tools.details import tool_detail

    return tool_detail(svc(request).sessions, tool_id,
                       timeout=svc(request).settings.workspace_execute_timeout)


@router.get("/api/tools/catalog")
def tool_catalog(request: Request):
    # 统一展示复用已有MCP记录；查看目录不会连接服务或赋予执行权限。
    with svc(request).sessions() as session:
        groups = read_groups(session)
    return catalog_view(MCPRegistry(svc(request).sessions).list(), groups=groups)


@router.get('/api/tools/groups')
def tool_groups(request: Request):
    return ToolGroupService(svc(request).sessions).list()


@router.post('/api/tools/groups', status_code=201)
def create_tool_group(body: GroupFields, request: Request):
    return ToolGroupService(svc(request).sessions).create(**body.model_dump())


@router.post('/api/tools/groups/{group_id}/preview')
def preview_tool_group(group_id: str, body: GroupEdit, request: Request):
    return ToolGroupService(svc(request).sessions).preview(group_id, **body.model_dump(exclude_unset=True))


@router.patch('/api/tools/groups/{group_id}')
def update_tool_group(group_id: str, body: GroupUpdate, request: Request):
    return ToolGroupService(svc(request).sessions).update(group_id, **body.model_dump(exclude_unset=True))


@router.delete('/api/tools/groups/{group_id}')
def delete_tool_group(group_id: str, body: GroupDelete, request: Request):
    return ToolGroupService(svc(request).sessions).update(group_id, deleted=True, **body.model_dump())


@router.get("/api/skills")
def skills(request: Request):
    from knowledgeops_agent.harness.runtime.resources import ResourceUnavailable, skill_catalog

    try:
        policy = ToolPolicyService(svc(request).sessions).get()
        return [
            {
                **present_skill(item),
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


def _skill_details(name: str, resource: str | None = None):
    from knowledgeops_agent.core.resources import resource_path
    from knowledgeops_agent.harness.runtime.resources import SKILLS_PATH
    from knowledgeops_agent.harness.skills.details import package_detail

    try:
        return package_detail(resource_path("SKILLS_DIRECTORY", SKILLS_PATH), name, resource=resource)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="技能包暂时无法读取，请检查目录及资源格式") from exc


@router.get("/api/skills/{name}")
def skill_details(name: str, request: Request):
    detail = _skill_details(name)
    policy = ToolPolicyService(svc(request).sessions).get()
    return {**present_skill(detail), "available": detail["enabled"],
            "enabled": detail["enabled"] and policy.get("skill_enabled", {}).get(name, True)}


@router.get("/api/skills/{name}/resource")
def skill_resource(name: str, path: str):
    return _skill_details(name, path)


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
