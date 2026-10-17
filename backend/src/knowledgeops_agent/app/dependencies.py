"""从当前 FastAPI 应用取得已装配服务，不在导入时创建连接。"""

from __future__ import annotations

from fastapi import Request

from knowledgeops_agent.app.services import Services


def svc(request: Request) -> Services:
    return request.app.state.services


def reasoning_service(request: Request):
    from knowledgeops_agent.harness.models.reasoning import ReasoningService

    dependencies = svc(request)
    if dependencies.reasoning is None:
        dependencies.reasoning = ReasoningService(dependencies.settings)
    return dependencies.reasoning
