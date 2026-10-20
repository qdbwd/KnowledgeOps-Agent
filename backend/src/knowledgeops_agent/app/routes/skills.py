"""技能文件包管理入口；上传仅处理字节，确认与权限开关相互独立。"""

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from knowledgeops_agent.app.dependencies import svc
from knowledgeops_agent.core.resources import managed_resource_root, resource_path
from knowledgeops_agent.harness.skills.imports import unpack_zip
from knowledgeops_agent.harness.skills.management import SkillManagement
from knowledgeops_agent.harness.skills.packages import (
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_FILES,
    MAX_FILE_BYTES,
    SkillPackageError,
)

router = APIRouter()


def manager(request: Request) -> SkillManagement:
    return SkillManagement(svc(request).sessions, resource_path("SKILLS_DIRECTORY", managed_resource_root("skills")))


class SkillRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)


class SkillRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(default="SKILL.md", max_length=512)
    offset: int = Field(default=0, ge=0)


@router.post("/api/skills/imports", status_code=201)
async def import_preview(request: Request):
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(415, "请上传SKILL.md、文件夹或ZIP包")

    async def bounded_stream():
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_BUNDLE_BYTES + 4 * MAX_FILE_BYTES:
                raise MultiPartException("上传请求超过大小限制")
            yield chunk

    try:
        form = await MultiPartParser(request.headers, bounded_stream(), max_files=MAX_BUNDLE_FILES,
                                     max_fields=3, max_part_size=2048).parse()
    except MultiPartException as exc:
        raise HTTPException(422, "上传格式无效或超过文件/大小限制") from exc
    try:
        files = []
        total = 0
        for key, value in form.multi_items():
            if isinstance(value, UploadFile):
                if key != "files":
                    raise HTTPException(422, "未知上传字段")
                data = await value.read(MAX_BUNDLE_BYTES + MAX_FILE_BYTES + 1)
                total += len(data)
                if total > MAX_BUNDLE_BYTES + MAX_FILE_BYTES:
                    raise HTTPException(422, "上传文件超过大小限制")
                files.append((value.filename or "", data))
            elif key not in {"name", "description", "expected_revision"}:
                raise HTTPException(422, "未知上传字段")
        name, description = form.get("name"), form.get("description")
        if any(value is not None and not isinstance(value, str) for value in [name, description]):
            raise HTTPException(422, "名称和用途必须为文本")
        try:
            revision_value = form.get("expected_revision", "0")
            if not isinstance(revision_value, str):
                raise ValueError()
            expected_revision = int(revision_value)
            if expected_revision < 0:
                raise ValueError()
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, "修订号无效") from exc
        if len(files) == 1 and files[0][0].lower().endswith(".zip"):
            files = await asyncio.to_thread(unpack_zip, files[0][1])
        return await asyncio.to_thread(manager(request).stage, files, name=name, description=description,
                                       expected_revision=expected_revision)
    except SkillPackageError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        await form.close()


@router.get("/api/skills/imports/{identity}")
def import_status(identity: str, request: Request):
    return manager(request).operation(identity)


@router.post("/api/skills/imports/{identity}/try-read")
def try_read_import(identity: str, body: SkillRead, request: Request):
    return manager(request).trial_read(identity, **body.model_dump())


@router.post("/api/skills/imports/{identity}/commit")
def commit_import(identity: str, request: Request):
    return manager(request).commit(identity)


@router.delete("/api/skills/imports/{identity}")
def discard_import(identity: str, request: Request):
    return manager(request).discard(identity)


@router.get("/api/skills")
def skills(request: Request):
    try:
        return manager(request).catalog()
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "技能配置不可用，请检查部署目录或重试恢复") from exc


@router.get("/api/skills/{name}")
def skill_details(name: str, request: Request):
    try:
        return manager(request).detail(name)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(503, "技能包暂时无法读取，请检查目录及资源格式") from exc


@router.get("/api/skills/{name}/resource")
def skill_resource(name: str, path: str, request: Request):
    try:
        return manager(request).detail(name, resource=path)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(503, "技能包暂时无法读取，请检查目录及资源格式") from exc


@router.get("/api/skills/{name}/export")
def export_skill(name: str, request: Request):
    data = manager(request).export(name)
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})


@router.delete("/api/skills/{name}")
def remove_skill(name: str, body: SkillRevision, request: Request):
    return manager(request).remove(name, expected_revision=body.expected_revision)
