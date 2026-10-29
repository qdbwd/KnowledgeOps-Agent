"""system HTTP 接口；业务处理委托给已有应用服务。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

from knowledgeops_agent.app.dependencies import reasoning_service, svc

router = APIRouter()


@router.get("/api/health")
def health(request: Request):
    """API 自身存活与 PG 可达；不依赖 Worker，供启动器判断基础可用。"""
    with svc(request).engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.get("/api/readiness")
def readiness(request: Request):
    """分层就绪：进程、基础依赖、执行就绪与积压；只读、有界、短缓存。"""
    return svc(request).readiness.summary()


@router.get("/api/readiness/runs/{run_id}")
def readiness_for_run(run_id: str, request: Request):
    """按现有领取规则解释单个任务为什么还在排队；不改变调度。"""
    try:
        return svc(request).readiness.blocking_reason(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


@router.get("/api/maintenance/backups")
def maintenance_backups(request: Request, limit: int = Query(default=20, ge=1, le=50)):
    """只读列出本机备份记录；不创建备份、不下载 dump、不覆盖正式库。"""
    return svc(request).backup_records.list(limit)


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
