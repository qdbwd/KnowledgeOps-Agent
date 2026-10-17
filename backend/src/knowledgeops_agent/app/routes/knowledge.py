"""knowledge HTTP 接口；业务处理委托给已有应用服务。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from knowledgeops_agent.app.dependencies import svc
from knowledgeops_agent.app.schemas import (
    DocumentActionInput,
    EditInput,
)

router = APIRouter()


@router.get("/api/knowledge")
def knowledge(request: Request, query: str = "", include_archived: bool = False):
    service = svc(request).knowledge
    return (
        service.search(query)
        if query.strip()
        else service.list_documents(include_archived=include_archived)
    )


@router.get("/api/knowledge/export")
def export(request: Request):
    data = svc(request).knowledge.export_markdown()
    return Response(
        data,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="knowledgeops-markdown.zip"',
        },
    )


@router.get("/api/knowledge/{document_id}")
def document(document_id: str, request: Request, version_id: str | None = None):
    return svc(request).knowledge.get_document(document_id, version_id=version_id)


@router.get("/api/knowledge/{document_id}/versions")
def versions(document_id: str, request: Request):
    return svc(request).knowledge.history(document_id)


@router.post("/api/knowledge/{document_id}/edits")
def edit_document(document_id: str, body: EditInput, request: Request):
    service = svc(request).knowledge
    root_id = "user-edit:" + body.request_key
    operation = {
        **body.model_dump(exclude={"request_key"}, exclude_unset=True),
        "action": "update",
        "document_id": document_id,
    }
    draft = service.create_draft(root_id, [operation], operation_key=root_id)
    if draft["status"] != "published":
        service.approve(draft["id"], expected_revision=draft["revision"])
    return service.publish(
        draft["id"], idempotency_key=root_id, expected_revision=draft["revision"]
    )


@router.post("/api/knowledge/{document_id}/actions", status_code=201)
def document_action(document_id: str, body: DocumentActionInput, request: Request):
    """页面归档/恢复先生成可审阅变更，与 Agent 和编辑共用版本事务。"""
    root_id = "user-action:" + body.request_key
    return svc(request).knowledge.create_draft(
        root_id,
        [{**body.model_dump(exclude={"request_key"}), "document_id": document_id}],
        operation_key=root_id,
    )
