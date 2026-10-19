"""changesets HTTP 接口；业务处理委托给已有应用服务。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from knowledgeops_agent.app.dependencies import svc
from knowledgeops_agent.app.schemas import (
    DraftInput,
    PublishInput,
    RevisionInput,
)

router = APIRouter()


@router.get("/api/changesets")
def changesets(request: Request, root_id: str | None = None):
    return svc(request).knowledge.list_drafts(root_id=root_id)


@router.get("/api/changesets/page")
def changesets_page(
    request: Request,
    root_id: str | None = None,
    conversation_id: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
):
    return svc(request).knowledge.drafts_page(
        root_id=root_id,
        conversation_id=conversation_id,
        status=status,
        cursor=cursor,
        limit=limit,
    )


@router.get("/api/changesets/{changeset_id}")
def changeset(changeset_id: str, request: Request):
    return svc(request).knowledge.get_draft(changeset_id)


@router.put("/api/changesets/{changeset_id}")
def edit_changeset(changeset_id: str, body: DraftInput, request: Request):
    return svc(request).knowledge.edit_draft(
        changeset_id, body.operations, expected_revision=body.expected_revision
    )


@router.post("/api/changesets/{changeset_id}/reject")
def reject(changeset_id: str, body: RevisionInput, request: Request):
    return svc(request).knowledge.reject(changeset_id, expected_revision=body.expected_revision)


@router.post("/api/changesets/{changeset_id}/save")
def save_changeset(changeset_id: str, body: RevisionInput, request: Request):
    return svc(request).knowledge.save_draft(changeset_id, expected_revision=body.expected_revision)


@router.post("/api/changesets/{changeset_id}/publish")
def publish(changeset_id: str, body: PublishInput, request: Request):
    service = svc(request).knowledge
    draft = service.get_draft(changeset_id)
    if draft["status"] != "published":
        service.approve(
            changeset_id,
            confirm_impact=body.confirm_impact,
            expected_revision=body.expected_revision,
        )
    return service.publish(
        changeset_id,
        idempotency_key=f"publish:{changeset_id}",
        expected_revision=body.expected_revision,
    )
