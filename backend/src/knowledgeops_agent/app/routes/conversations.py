"""conversations HTTP 接口；业务处理委托给已有应用服务。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from knowledgeops_agent.app.dependencies import reasoning_service, svc
from knowledgeops_agent.app.schemas import (
    ConversationInput,
    ConversationRenameInput,
    MessageInput,
)

router = APIRouter()


class ConversationDeletionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


def _deletion_view(row):
    # 公开进度不包含领取凭据、内部游标或文件清单。
    return {key: row[key] for key in ("id", "target_id", "status", "phase", "summary", "last_error", "created_at", "updated_at", "finished_at")}


@router.get("/api/conversations/{conversation_id}/deletion-preview")
def deletion_preview(conversation_id: str, request: Request):
    return svc(request).lifecycle.conversations.preview(conversation_id)


@router.delete("/api/conversations/{conversation_id}", status_code=202)
def delete_conversation(conversation_id: str, body: ConversationDeletionInput, request: Request):
    try:
        return _deletion_view(svc(request).lifecycle.conversations.admit(conversation_id, expected_version=body.expected_version))
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


def _conversation_job(conversation_id, request):
    from sqlalchemy import select

    from knowledgeops_agent.app.lifecycle.conversations import completed_deletion_view
    from knowledgeops_agent.app.lifecycle.jobs import job_view
    from knowledgeops_agent.persistence.models import CleanupJob, Conversation

    with svc(request).sessions() as session:
        row = session.scalar(select(CleanupJob).where(CleanupJob.target_kind == "conversation", CleanupJob.target_id == conversation_id))
        if row is not None:
            return job_view(row)
        conversation = session.get(Conversation, conversation_id)
        if conversation is not None and conversation.deletion_state == "deleted":
            # 已完成作业按期限回收后，墓碑仍能回答删除是否完成。
            return completed_deletion_view(conversation)
        raise KeyError("conversation deletion not found")


@router.get("/api/conversations/{conversation_id}/deletion")
def deletion_status(conversation_id: str, request: Request):
    return _deletion_view(_conversation_job(conversation_id, request))


@router.post("/api/conversations/{conversation_id}/deletion/retry", status_code=202)
def retry_deletion(conversation_id: str, request: Request):
    row = _conversation_job(conversation_id, request)
    if row["id"] is not None:
        row = svc(request).lifecycle.conversations.retry(row["id"])
    return _deletion_view(row)


@router.get("/api/conversations")
def conversations(request: Request):
    return svc(request).jobs.conversations()


@router.post("/api/conversations", status_code=201)
def create_conversation(body: ConversationInput, request: Request):
    return svc(request).jobs.create_conversation(body.title)


@router.get("/api/conversations/{conversation_id}/messages")
def messages(conversation_id: str, request: Request):
    return svc(request).jobs.messages(conversation_id)


@router.patch("/api/conversations/{conversation_id}")
def rename_conversation(conversation_id: str, body: ConversationRenameInput, request: Request):
    return svc(request).jobs.rename_conversation(conversation_id, body.title)


@router.get("/api/conversations/{conversation_id}/context-usage")
def conversation_context_usage(conversation_id: str, request: Request):
    from knowledgeops_agent.harness.context.usage import context_usage

    dependencies = svc(request)
    return context_usage(dependencies.sessions, dependencies.gateway, conversation_id)


@router.post("/api/conversations/{conversation_id}/messages", status_code=202)
def send_message(conversation_id: str, body: MessageInput, request: Request):
    dependencies = svc(request)
    from knowledgeops_agent.harness.permissions.settings import ExecutionSettings

    defaults = ExecutionSettings(dependencies.sessions).get(conversation_id)
    for source_id in body.source_ids:
        dependencies.sources.get_source(source_id)
    return dependencies.jobs.enqueue(
        conversation_id,
        body.content,
        request_key=body.request_key,
        publication_mode=body.publication_mode,
        knowledge_only=body.knowledge_only,
        delegation=body.delegation,
        auxiliary=body.auxiliary,
        reasoning=reasoning_service(request).freeze(
            body.reasoning_effort or defaults["reasoning_effort"]
        ),
        inherit_reasoning=body.reasoning_effort is None,
        expected_runtime_revision=defaults["revision"],
        source_ids=body.source_ids,
        budgets=body.budgets.model_dump(),
        learning_task_id=body.learning_task_id,
    )


class CompactionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: str = Field(min_length=1, max_length=200)


@router.get("/api/conversations/{conversation_id}/context-compaction")
def compaction_status(conversation_id: str, request: Request):
    from knowledgeops_agent.harness.context.manual import status

    dependencies = svc(request)
    return status(dependencies.jobs, dependencies.gateway, conversation_id)


@router.post("/api/conversations/{conversation_id}/context-compaction", status_code=202)
def compact_conversation(conversation_id: str, body: CompactionInput, request: Request):
    from knowledgeops_agent.harness.context.manual import enqueue

    dependencies = svc(request)
    try:
        return enqueue(dependencies.jobs, dependencies.gateway, conversation_id, body.request_key)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
