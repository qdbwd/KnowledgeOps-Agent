"""runs HTTP 接口；业务处理委托给已有应用服务。"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from knowledgeops_agent.app.dependencies import svc
from knowledgeops_agent.app.runs.children import ChildRuns
from knowledgeops_agent.app.runs.details import task_details
from knowledgeops_agent.app.runs.events import iter_run_events
from knowledgeops_agent.app.runs.questions import QuestionAnswer, RunQuestions
from knowledgeops_agent.app.runs.service import Budgets
from knowledgeops_agent.app.schemas import (
    GuidanceInput,
    ResumeInput,
)
from knowledgeops_agent.harness.mcp.calls import MCPCalls
from knowledgeops_agent.harness.runtime.resources import ResourceService

router = APIRouter()


class ChildInstructionInput(BaseModel):
    """HTTP 入口只接收真人输入，来源不能由客户端伪造。"""

    model_config = ConfigDict(extra="forbid")
    request_key: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=20000)


@router.get("/api/runs/{run_id}/children")
def children(run_id: str, request: Request):
    return ChildRuns(svc(request).jobs).list(run_id)


@router.get("/api/runs/{run_id}/children/{child_id}")
def child(run_id: str, child_id: str, request: Request):
    return ChildRuns(svc(request).jobs).get(run_id, child_id)


@router.get("/api/runs/{run_id}/children/{child_id}/instructions")
def child_instructions(run_id: str, child_id: str, request: Request):
    return ChildRuns(svc(request).jobs).instructions(run_id, child_id)


@router.post("/api/runs/{run_id}/children/{child_id}/instructions")
def instruct_child(run_id: str, child_id: str, body: ChildInstructionInput, request: Request):
    return ChildRuns(svc(request).jobs).update(
        run_id, child_id, body.message, request_key=body.request_key, origin="human",
    )


@router.post("/api/runs/{run_id}/children/{child_id}/cancel")
def cancel_child(run_id: str, child_id: str, request: Request):
    return ChildRuns(svc(request).jobs).cancel(run_id, child_id)


@router.get("/api/runs")
def runs(request: Request, conversation_id: str | None = None, root_id: str | None = None):
    return svc(request).jobs.list_runs(conversation_id=conversation_id, root_id=root_id)


@router.get("/api/runs/page")
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


@router.get("/api/runs/{run_id}")
def run(run_id: str, request: Request):
    dependencies = svc(request)
    result = dependencies.jobs.get_run(run_id)
    result["model_usage"] = dependencies.gateway.ledger.summary(result["root_id"])
    return result


@router.get("/api/runs/{run_id}/records")
def run_records(run_id: str, request: Request, after: Annotated[int, Query(ge=0)] = 0):
    dependencies = svc(request)
    row = dependencies.jobs.get_run(run_id)
    if not row["details_available"]:
        return {"run": row, "models": [], "events": [],
                "event_page": {"after": after, "next_after": None, "total": 0, "expired": True},
                "artifacts": dependencies.jobs.artifacts(run_id), "resources": None,
                "continuation": None, "guidance": [], "mcp_calls": []}
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


@router.get("/api/runs/{run_id}/summary")
def run_summary(run_id: str, request: Request):
    return task_details(svc(request).sessions, run_id)


@router.get("/api/runs/{run_id}/artifacts/{artifact_id}")
def run_artifact(run_id: str, artifact_id: str, request: Request, start: int = 0):
    jobs = svc(request).jobs
    source_url = jobs.artifact_source_url(run_id, artifact_id)
    if source_url:
        return {"source_url": source_url}
    return jobs.read_artifact(artifact_id, root_id=jobs.get_run(run_id)["root_id"], start=start)


@router.get("/api/runs/{run_id}/events")
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


@router.get("/api/runs/{run_id}/guidance")
def guidance(run_id: str, request: Request):
    jobs = svc(request).jobs
    return jobs.guidance(jobs.get_run(run_id)["root_id"])


@router.post("/api/runs/{run_id}/guide")
def guide(run_id: str, body: GuidanceInput, request: Request):
    return svc(request).jobs.guide(
        run_id, target_id=body.target_root_id, target_generation=body.target_generation
    )


@router.post("/api/runs/{run_id}/cancel")
def cancel(run_id: str, request: Request):
    return svc(request).jobs.cancel(run_id)


@router.get("/api/runs/{run_id}/questions")
def questions(run_id: str, request: Request):
    jobs = svc(request).jobs
    return RunQuestions(jobs).list(jobs.get_run(run_id)["root_id"])


@router.post("/api/runs/{run_id}/questions/{question_id}/answer")
def answer_question(run_id: str, question_id: str, body: QuestionAnswer, request: Request):
    jobs = svc(request).jobs
    return RunQuestions(jobs).answer(jobs.get_run(run_id)["root_id"], question_id, body)


@router.post("/api/runs/{run_id}/pause")
def pause(run_id: str, request: Request):
    return svc(request).jobs.pause(run_id)


@router.post("/api/runs/{run_id}/resume", status_code=202)
def resume(run_id: str, body: ResumeInput, request: Request):
    dependencies = svc(request)
    row = dependencies.jobs.get_run(run_id)
    if not row["details_available"]:
        raise HTTPException(410, "运行详情已过期或正在清理，不能原地恢复；请创建新任务。")
    if row["parent_id"] or row["status"] not in {
        "failed",
        "cancelled",
        "interrupted",
        "budget_exhausted",
        "paused",
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


@router.get("/api/runs/{run_id}/export")
def run_export(run_id: str, request: Request):
    records = run_records(run_id, request)
    if not records["run"]["details_available"]:
        raise HTTPException(410, "完整运行轨迹已过期或正在清理，无法导出。")
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
