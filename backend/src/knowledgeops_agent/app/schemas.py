"""HTTP 输入合同；保持已有校验、字段及 OpenAPI 名称。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from knowledgeops_agent.app.runs.service import Budgets
from knowledgeops_agent.memory.service import MemoryCategory


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ConversationInput(InputModel):
    title: str = Field(default="新对话", max_length=300)


class GuidanceInput(InputModel):
    target_root_id: str = Field(min_length=1, max_length=120)
    target_generation: int = Field(ge=1)


class PreferenceInput(InputModel):
    content: str = Field(min_length=1, max_length=2000)
    scope: Literal["global", "topic"] = "global"
    topic: str = Field(default="", max_length=200)


class PreferenceUpdate(InputModel):
    expected_revision: int = Field(ge=1)
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    scope: Literal["global", "topic"] | None = None
    topic: str | None = Field(default=None, max_length=200)
    status: Literal["active", "pending", "inactive", "deleted"] | None = None
    supersedes_id: str | None = Field(default=None, max_length=36)


class MemoryInput(PreferenceInput):
    category: MemoryCategory = "preference"


class MemoryUpdate(PreferenceUpdate):
    category: MemoryCategory | None = None


class MemoryRollback(InputModel):
    expected_revision: int = Field(ge=1)
    target_revision: int = Field(ge=1)


class MemoryMerge(InputModel):
    revisions: dict[str, int] = Field(min_length=2, max_length=10)
    content: str = Field(min_length=1, max_length=2000)


class MemoryExpectedRevision(InputModel):
    expected_revision: int = Field(ge=1)


class MessageInput(InputModel):
    content: str = Field(min_length=1, max_length=100000)
    request_key: str = Field(min_length=1, max_length=200)
    publication_mode: Literal["ask", "preview", "direct"] = "ask"
    knowledge_only: bool = False
    delegation: bool = True
    auxiliary: bool = Field(default=False, strict=True)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=32)
    source_ids: list[str] = Field(default_factory=list, max_length=30)
    budgets: Budgets = Field(default_factory=Budgets)
    learning_task_id: str | None = Field(default=None, min_length=1, max_length=36)


class MCPInput(InputModel):
    name: str = Field(min_length=1, max_length=200)
    config: dict


class MCPUpdate(InputModel):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict | None = None
    enabled: bool | None = None
    permissions: dict[str, Literal["deny", "read", "confirm"]] | None = None
    deleted: bool = False


class MCPDecision(InputModel):
    expected_hash: str = Field(min_length=1, max_length=64)
    approve: bool


class TextInput(InputModel):
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=2000000)


class UrlInput(InputModel):
    url: str = Field(min_length=1, max_length=4096)
    request_key: str = Field(min_length=1, max_length=160)
    refresh: bool = False


class RevisionInput(InputModel):
    expected_revision: int = Field(ge=1)


class PublishInput(RevisionInput):
    confirm_impact: bool = False


class DraftInput(RevisionInput):
    operations: list[dict] = Field(min_length=1, max_length=20)


class ResumeInput(InputModel):
    budgets: dict[str, int] = Field(default_factory=dict)


class EditInput(InputModel):
    request_key: str = Field(min_length=1, max_length=160)
    base_version_id: str
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=200000)
    summary: str = Field(default="", max_length=3000)
    category: str = Field(default="", max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=30)
    relations: list[dict[str, str]] = Field(default_factory=list, max_length=50)


class DocumentActionInput(InputModel):
    action: Literal["archive", "restore"]
    request_key: str = Field(min_length=1, max_length=160)
    base_version_id: str
    restore_version_id: str | None = None
