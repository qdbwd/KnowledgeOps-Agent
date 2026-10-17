"""system HTTP 接口；业务处理委托给已有应用服务。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text

from knowledgeops_agent.app.dependencies import reasoning_service, svc

router = APIRouter()


@router.get("/api/health")
def health(request: Request):
    with svc(request).engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.get("/api/settings")
def settings(request: Request):
    dependencies = svc(request)
    return {
        "model": dependencies.settings.agent_model,
        "model_calls_enabled": dependencies.gateway.enabled,
        "data_scope": "verification"
        if dependencies.engine.url.database == "knowledgeops_test"
        else "project",
    }


@router.get("/api/model-capabilities")
def model_capabilities(request: Request):
    return reasoning_service(request).catalog()
