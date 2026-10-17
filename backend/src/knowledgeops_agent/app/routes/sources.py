"""sources HTTP 接口；业务处理委托给已有应用服务。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from knowledgeops_agent.app.dependencies import svc
from knowledgeops_agent.app.schemas import (
    TextInput,
    UrlInput,
)
from knowledgeops_agent.sources.web import public_url

router = APIRouter()


@router.get("/api/sources")
def sources(request: Request):
    return svc(request).sources.list_sources()


@router.post("/api/sources/text", status_code=201)
def source_text(body: TextInput, request: Request):
    return svc(request).sources.import_text(body.title, body.content)


@router.post("/api/sources/url", status_code=202)
def source_url(body: UrlInput, request: Request):
    return svc(request).jobs.enqueue_material(
        {"type": "web", "url": str(public_url(body.url)), "refresh": body.refresh},
        request_key=body.request_key,
    )


@router.post("/api/sources/upload", status_code=201)
async def upload(request: Request, file: Annotated[UploadFile, File()]):
    data = await file.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "maximum upload size is 10 MiB")
    title = Path(file.filename or "材料.txt").name[:500]
    if Path(title).suffix.lower() == ".pdf":
        source = await asyncio.to_thread(svc(request).sources.register_pdf, title, data)
        run = await asyncio.to_thread(
            svc(request).jobs.enqueue_material,
            {"type": "pdf", "version_id": source["version_id"]},
            request_key="pdf:" + source["version_id"],
        )
        return JSONResponse({"source_id": source["id"], "job_id": run["id"]}, status_code=202)
    if Path(title).suffix.lower() not in {".md", ".txt"}:
        raise HTTPException(422, "supported uploads: Markdown, TXT and PDF")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, "text file must use UTF-8 encoding") from None
    return await asyncio.to_thread(
        svc(request).sources.import_text, title, content, kind="file", raw_bytes=data
    )


@router.get("/api/sources/{source_id}")
def source(source_id: str, request: Request, version_id: str | None = None):
    return svc(request).sources.get_source(source_id, version_id=version_id)


@router.get("/api/source-versions/{version_id}/original")
def source_original(version_id: str, request: Request):
    data, mime = svc(request).sources.original(version_id)
    return Response(
        data,
        media_type=mime,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "attachment"
            if mime in {"text/html", "application/xhtml+xml"}
            else "inline",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


@router.get("/api/source-versions/{version_id}/content")
def source_content(version_id: str, request: Request):
    return svc(request).sources.full_content(version_id)


@router.delete("/api/sources/{source_id}")
def delete_source(source_id: str, request: Request):
    return svc(request).sources.remove_source(source_id)


@router.get("/api/source-segments/{segment_id}")
def source_segment(segment_id: str, request: Request):
    return svc(request).sources.get_segment(segment_id)


@router.get("/api/evidence/{evidence_id}")
def evidence(evidence_id: str, request: Request):
    return svc(request).sources.get_evidence(evidence_id)
