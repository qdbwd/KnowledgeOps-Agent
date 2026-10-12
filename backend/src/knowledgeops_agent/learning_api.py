"""学习任务与配置的HTTP入口；与聊天控制共用服务，UI不直接改数据库。"""

from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from knowledgeops_agent.learning_profiles import LearningProfileService
from knowledgeops_agent.learning_tasks import (
    LearningTaskService,
    TaskConfiguration,
    TaskFields,
    UnitInput,
)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class TaskCreate(TaskFields):
    conversation_id: str = Field(min_length=1, max_length=36)
    request_key: str = Field(min_length=1, max_length=200)


class TaskUpdate(Input):
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    goal: str | None = Field(default=None, min_length=1, max_length=10000)
    scope: str | None = Field(default=None, max_length=10000)
    topic: str | None = Field(default=None, max_length=200)
    mode: Literal["interactive", "delegated"] | None = None
    units: list[UnitInput] | None = Field(default=None, max_length=50)


class Expected(Input):
    expected_revision: int = Field(ge=1)


class Confirm(Expected):
    auto_publish: bool = Field(default=False, strict=True)


class ConfigurationUpdate(Expected):
    changes: TaskConfiguration


class ConfigurationReuse(Expected):
    source_task_id: str = Field(min_length=1, max_length=36)
    source_revision: int = Field(ge=1)


class ProfileUpdate(Expected):
    content: str | None = Field(default=None, max_length=20000)
    enabled: bool | None = Field(default=None, strict=True)


class ProfileImport(Expected):
    filename: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=20000)


class ImportApply(Input):
    preview_id: str = Field(min_length=1, max_length=36)
    expected_hash: str = Field(min_length=64, max_length=64)


class ProfileRollback(Expected):
    target_revision: int = Field(ge=1)


def learning_router() -> APIRouter:
    router = APIRouter(prefix="/api")

    def tasks(request):
        return LearningTaskService(request.app.state.services.sessions)

    def profiles(request):
        return LearningProfileService(request.app.state.services.sessions)

    @router.get("/learning-tasks")
    def list_tasks(request: Request, conversation_id: str | None = None):
        return tasks(request).list(conversation_id=conversation_id)

    @router.post("/learning-tasks", status_code=201)
    def create_task(body: TaskCreate, request: Request):
        return tasks(request).create(**body.model_dump())

    @router.get("/learning-tasks/{task_id}")
    def get_task(task_id: str, request: Request):
        return tasks(request).get(task_id)

    @router.patch("/learning-tasks/{task_id}")
    def update_task(task_id: str, body: TaskUpdate, request: Request):
        return tasks(request).update(task_id, **body.model_dump(exclude_unset=True))

    @router.get("/learning-tasks/{task_id}/history")
    def task_history(task_id: str, request: Request):
        return tasks(request).history(task_id)

    @router.get("/learning-tasks/{task_id}/collaboration")
    def collaboration(task_id: str, request: Request):
        from knowledgeops_agent.learning_collaboration import LearningCollaborationService

        tasks(request).get(task_id)
        return LearningCollaborationService(request.app.state.services.sessions).list(task_id)

    @router.get("/learning-tasks/{task_id}/advances")
    def advances(task_id: str, request: Request):
        from knowledgeops_agent.learning_coordinator import view_advances

        tasks(request).get(task_id)
        return view_advances(request.app.state.services.sessions, task_id)

    @router.post("/learning-tasks/{task_id}/select")
    def select_task(task_id: str, request: Request):
        return tasks(request).select(task_id)

    @router.post("/learning-tasks/{task_id}/confirm")
    def confirm_task(task_id: str, body: Confirm, request: Request):
        return tasks(request).confirm(task_id, **body.model_dump())

    @router.post("/learning-tasks/{task_id}/configuration")
    def configure_task(task_id: str, body: ConfigurationUpdate, request: Request):
        return tasks(request).configure(
            task_id,
            expected_revision=body.expected_revision,
            changes=body.changes.model_dump(exclude_unset=True),
            source={"kind": "manual"},
        )

    @router.post("/learning-tasks/{task_id}/configuration-reuse")
    def reuse_task_configuration(task_id: str, body: ConfigurationReuse, request: Request):
        return tasks(request).reuse_configuration(task_id, **body.model_dump())

    @router.post("/learning-tasks/{task_id}/{action}")
    def control_task(
        task_id: str, action: Literal["pause", "resume", "cancel"], body: Expected, request: Request
    ):
        return tasks(request).control(task_id, action=action, **body.model_dump())

    @router.get("/learning-profiles")
    def get_profile(request: Request, task_id: str | None = None):
        return profiles(request).get(task_id=task_id)

    @router.get("/learning-profiles/effective")
    def effective_profile(
        request: Request,
        task_id: str | None = None,
        query: str = Query(default="学习", max_length=500),
    ):
        service = profiles(request)
        return service.effective(service.freeze(task_id=task_id, query=query))

    @router.get("/learning-profiles/effective/export")
    def export_effective_profile(request: Request, task_id: str | None = None):
        return Response(
            profiles(request).export_effective(task_id=task_id),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="reader-profile.md"'},
        )

    @router.get("/learning-profiles/{profile_id}")
    def profile_detail(profile_id: str, request: Request):
        return profiles(request).by_id(profile_id)

    @router.patch("/learning-profiles/{profile_id}")
    def edit_profile(profile_id: str, body: ProfileUpdate, request: Request):
        return profiles(request).update(profile_id, **body.model_dump(exclude_unset=True))

    @router.get("/learning-profiles/{profile_id}/history")
    def profile_history(profile_id: str, request: Request):
        return profiles(request).history(profile_id)

    @router.post("/learning-profiles/{profile_id}/rollback")
    def restore_profile(profile_id: str, body: ProfileRollback, request: Request):
        return profiles(request).rollback(profile_id, **body.model_dump())

    @router.post("/learning-profiles/{profile_id}/import-preview")
    def import_preview(profile_id: str, body: ProfileImport, request: Request):
        return profiles(request).preview_import(profile_id, **body.model_dump())

    @router.post("/learning-profiles/{profile_id}/import-apply")
    def import_apply(profile_id: str, body: ImportApply, request: Request):
        return profiles(request).apply_import(profile_id, **body.model_dump())

    @router.get("/learning-profiles/{profile_id}/export")
    def export_profile(profile_id: str, request: Request):
        return Response(
            profiles(request).export(profile_id),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="learning-profile.md"'},
        )

    return router
