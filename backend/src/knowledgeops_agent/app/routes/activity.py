"""跨页面活动摘要；所有处理动作仍跳回原任务或管理入口。"""

from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from knowledgeops_agent.app.activity import ApplicationActivity
from knowledgeops_agent.app.dependencies import svc

router = APIRouter()


class ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0, le=2**63 - 1, strict=True)


@router.get("/api/activity")
def activity(request: Request, conversation_ids: str = Query(default="", max_length=1480)):
    return ApplicationActivity(svc(request).sessions).summary(filter(None, conversation_ids.split(",")))


@router.get("/api/activity/runs")
def runs(request: Request, view: Literal["attention", "active", "unread", "all"] = "attention",
         cursor: str | None = None, limit: int = Query(default=20, ge=1, le=100)):
    return ApplicationActivity(svc(request).sessions).page(view=view, cursor=cursor, limit=limit)


@router.get("/api/activity/management")
def management(request: Request, kind: Literal["mcp", "deletion"],
               target_kind: Literal["conversation", "source", "delivery", "draft", "knowledge", "run"] | None = None,
               cursor: str | None = Query(default=None, max_length=512), limit: int = Query(default=20, ge=1, le=100)):
    return ApplicationActivity(svc(request).sessions).management_page(kind=kind, target_kind=target_kind, cursor=cursor, limit=limit)


@router.post("/api/activity/runs/{run_id}/read")
def mark_read(run_id: str, body: ReadInput, request: Request):
    return ApplicationActivity(svc(request).sessions).mark_read(run_id, body.version)
