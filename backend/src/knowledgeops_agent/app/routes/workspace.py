"""workspace HTTP 接口；业务处理委托给已有应用服务。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from knowledgeops_agent.app.dependencies import svc

router = APIRouter()


@router.get("/api/workspace/download")
def workspace_download(request: Request, path: str = Query(max_length=2000)):
    """只传输挂载区交付文件，不执行文件、不开放宿主任意路径。"""
    from knowledgeops_agent.harness.sandbox.tools import workspace_file

    root = svc(request).settings.workspace_root
    if root is None:
        raise HTTPException(404, "workspace is not configured")
    target = workspace_file(root, path, delivery=True)
    return FileResponse(
        target,
        filename=target.name,
        media_type="application/octet-stream",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


@router.get("/api/workspace/preview")
def workspace_preview(request: Request, path: str = Query(max_length=2000)):
    """沿用交付文件白名单；完整文本最多2MiB，二进制或更大文件提供下载。"""
    from knowledgeops_agent.harness.sandbox.tools import workspace_file

    root = svc(request).settings.workspace_root
    if root is None:
        raise HTTPException(404, "workspace is not configured")
    target = workspace_file(root, path, delivery=True)
    with target.open("rb") as stream:
        data = stream.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024 or b"\0" in data:
        return {"name": target.name, "text": None, "reason": "此文件较大或属于二进制格式，请下载后查看。"}
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return {"name": target.name, "text": None, "reason": "此文件不是UTF-8文本，请下载后查看。"}
    return {"name": target.name, "text": content, "markdown": target.suffix.lower() in {".md", ".markdown"}}
