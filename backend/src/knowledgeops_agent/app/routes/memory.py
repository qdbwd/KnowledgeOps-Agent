"""memory HTTP 接口；业务处理委托给已有应用服务。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from knowledgeops_agent.app.dependencies import svc
from knowledgeops_agent.app.schemas import (
    MemoryCategoryInput,
    MemoryCategoryUpdate,
    MemoryClassificationInput,
    MemoryExpectedRevision,
    MemoryInput,
    MemoryMerge,
    MemoryRollback,
    MemoryUpdate,
    PreferenceInput,
    PreferenceUpdate,
)
from knowledgeops_agent.memory.categories import MemoryCategories
from knowledgeops_agent.memory.service import MemoryService, PreferenceService

router = APIRouter()


@router.get("/api/preferences")
def preferences(request: Request, include_deleted: bool = False):
    return PreferenceService(svc(request).sessions).list(include_deleted=include_deleted)


@router.post("/api/preferences", status_code=201)
def create_preference(body: PreferenceInput, request: Request):
    return PreferenceService(svc(request).sessions).create(**body.model_dump())


@router.patch("/api/preferences/{preference_id}")
def edit_preference(preference_id: str, body: PreferenceUpdate, request: Request):
    return PreferenceService(svc(request).sessions).update(
        preference_id, **body.model_dump(exclude_unset=True)
    )


@router.get("/api/preferences/{preference_id}/history")
def preference_history(preference_id: str, request: Request):
    return PreferenceService(svc(request).sessions).history(preference_id)


@router.get("/api/memories")
def memories(request: Request, include_deleted: bool = False):
    return MemoryService(svc(request).sessions).list(include_deleted=include_deleted)


@router.get("/api/memories/search")
def search_memories(
    request: Request,
    query: str = Query(min_length=1, max_length=500),
    topic: str = Query(default="", max_length=200),
    limit: int = Query(default=8, ge=1, le=50),
):
    return MemoryService(svc(request).sessions).search(query, topic=topic, limit=limit)


@router.post("/api/memories", status_code=201)
def create_memory(body: MemoryInput, request: Request):
    from knowledgeops_agent.memory import classification

    services = svc(request)
    values = body.model_dump()
    values["category"] = body.category or "preference"
    if body.category is not None and "display_category_id" not in body.model_fields_set:
        values.pop("display_category_id")  # 旧客户端的六类选择仍映射为对应展示类别。
    return MemoryService(
        services.sessions,
        on_created=lambda session, row: classification.queue_classification(
            session, services.jobs, services.gateway, row, f"initial:{row.id}:{row.revision}"
        ),
    ).create(**values)


@router.post("/api/memories/{memory_id}/classify", status_code=202)
def classify_memory(memory_id: str, body: MemoryClassificationInput, request: Request):
    from knowledgeops_agent.memory.classification import retry

    services = svc(request)
    return retry(services.jobs, services.gateway, memory_id, **body.model_dump())


@router.get("/api/memory-categories")
def memory_categories(request: Request):
    return MemoryCategories(svc(request).sessions).list()


@router.post("/api/memory-categories", status_code=201)
def create_memory_category(body: MemoryCategoryInput, request: Request):
    return MemoryCategories(svc(request).sessions).create(body.name)


@router.patch("/api/memory-categories/{category_id}")
def edit_memory_category(category_id: str, body: MemoryCategoryUpdate, request: Request):
    return MemoryCategories(svc(request).sessions).update(category_id, **body.model_dump())


@router.delete("/api/memory-categories/{category_id}")
def delete_memory_category(category_id: str, body: MemoryExpectedRevision, request: Request):
    return MemoryCategories(svc(request).sessions).delete(category_id, **body.model_dump())


@router.post("/api/memories/merge", status_code=201)
def merge_memories(body: MemoryMerge, request: Request):
    return MemoryService(svc(request).sessions).merge(**body.model_dump())


@router.get("/api/memories/{memory_id}")
def memory_detail(memory_id: str, request: Request):
    return MemoryService(svc(request).sessions).get(memory_id)


@router.patch("/api/memories/{memory_id}")
def edit_memory(memory_id: str, body: MemoryUpdate, request: Request):
    return MemoryService(svc(request).sessions).update(
        memory_id, **body.model_dump(exclude_unset=True)
    )


@router.get("/api/memories/{memory_id}/history")
def memory_history(memory_id: str, request: Request):
    return MemoryService(svc(request).sessions).history(memory_id)


@router.post("/api/memories/{memory_id}/rollback")
def rollback_memory(memory_id: str, body: MemoryRollback, request: Request):
    return MemoryService(svc(request).sessions).rollback(memory_id, **body.model_dump())


@router.post("/api/memories/{memory_id}/undo-merge")
def undo_memory_merge(memory_id: str, body: MemoryExpectedRevision, request: Request):
    return MemoryService(svc(request).sessions).undo_merge(memory_id, **body.model_dump())
