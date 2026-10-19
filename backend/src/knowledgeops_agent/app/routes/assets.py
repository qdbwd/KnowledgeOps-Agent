"""资产删除预览、明确确认与持久进度；不在HTTP请求中执行文件回收。"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from knowledgeops_agent.app.dependencies import svc
from knowledgeops_agent.app.lifecycle.asset_cleanup import public_job

router = APIRouter()
AssetKind = Literal["source", "delivery"]


class AssetDeletionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


def accept_deletion(request, kind, identity, body, conversation_id=None):
    try:
        return public_job(svc(request).lifecycle.assets.admit(kind, identity,
            expected_version=body.expected_version, conversation_id=conversation_id))
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.get("/api/assets/{kind}/{identity}/deletion-preview")
def asset_deletion_preview(kind: AssetKind, identity: str, request: Request, conversation_id: str | None = None):
    return svc(request).lifecycle.assets.preview(kind, identity, conversation_id=conversation_id)


@router.post("/api/assets/{kind}/{identity}/deletion", status_code=202)
def asset_deletion(kind: AssetKind, identity: str, body: AssetDeletionInput, request: Request,
                   conversation_id: str | None = None):
    return accept_deletion(request, kind, identity, body, conversation_id)


@router.get("/api/asset-deletions")
def asset_deletions(request: Request, limit: int = Query(default=20, ge=1, le=100)):
    return svc(request).lifecycle.assets.list_jobs(limit=limit)


@router.get("/api/asset-deletions/{identity}")
def asset_deletion_status(identity: str, request: Request):
    return svc(request).lifecycle.assets.get_job(identity)


@router.post("/api/asset-deletions/{identity}/retry", status_code=202)
def retry_asset_deletion(identity: str, request: Request):
    return svc(request).lifecycle.assets.retry(identity)
