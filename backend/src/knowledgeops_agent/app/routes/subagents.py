"""subagents HTTP 接口；业务处理委托给已有应用服务。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from knowledgeops_agent.app.dependencies import svc
from knowledgeops_agent.harness.subagents.registry import (
    SubagentCreate,
    SubagentRegistry,
    SubagentUpdate,
)

router = APIRouter()


@router.get("/api/subagents")
def subagents(request: Request):
    return SubagentRegistry(svc(request).sessions).list()


@router.get("/api/subagents/options")
def subagent_options():
    return SubagentRegistry.options()


@router.post("/api/subagents", status_code=201)
def create_subagent(body: SubagentCreate, request: Request):
    return SubagentRegistry(svc(request).sessions).create(**body.model_dump())


@router.patch("/api/subagents/{definition_id}")
def update_subagent(definition_id: str, body: SubagentUpdate, request: Request):
    return SubagentRegistry(svc(request).sessions).update(
        definition_id, **body.model_dump(exclude_unset=True)
    )


@router.get("/api/subagents/{definition_id}/history")
def subagent_history(definition_id: str, request: Request):
    return SubagentRegistry(svc(request).sessions).history(definition_id)
