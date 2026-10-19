"""知识正文与变更事务。Agent 工具和用户编辑都调用同一服务。

模型只能准备变更，不能在参数里声明自己已获得授权。发布从持久授权读取
用户决定，并对版本、证据和高影响操作进行检查；回执与正文一起提交。
"""

from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Callable
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert

from knowledgeops_agent.core.pagination import page_rows
from knowledgeops_agent.knowledge.citations import evidence_ids, render_citation_markers
from knowledgeops_agent.persistence.models import (
    ChangeSet,
    ChangeSetRevision,
    Citation,
    Evidence,
    KnowledgeDocument,
    KnowledgeRelation,
    KnowledgeSearchChunk,
    KnowledgeVersion,
    OperationReceipt,
    PublishReceipt,
    Run,
    SearchEntry,
    TaskGrant,
    new_id,
    utcnow,
)
from knowledgeops_agent.retrieval.index import add_knowledge_chunks
from knowledgeops_agent.retrieval.search import (
    LexicalQuery,
    excerpt,
    search_expressions,
    weighted_vector,
)


class DocumentChange(BaseModel):
    """更新必须声明基础版本；元数据未提供则继承，明确空值才表示清空。

    利用model_fields_set区分缺省和清空，在草稿落库前解析成固定值，
    避免预览与发布使用不同的动态默认值。正文和标题仍要求显式完整提供。
    """

    action: Literal["create", "update", "archive", "restore"]
    document_id: str | None = None
    base_version_id: str | None = None
    restore_version_id: str | None = None
    title: str = Field(default="", max_length=500)
    body: str = Field(default="", max_length=200000)
    summary: str = Field(default="", max_length=3000)
    category: str = Field(default="", max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=30)
    relations: list[dict[str, str]] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_action(self):
        if self.action in {"create", "update"} and (
            not self.title.strip() or not self.body.strip()
        ):
            raise ValueError("create/update requires a nonempty title and body")
        if self.action != "create" and (not self.document_id or not self.base_version_id):
            raise ValueError("update/archive/restore requires document and base version")
        if self.action == "restore" and not self.restore_version_id:
            raise ValueError("restore requires a historical version")
        for value in (self.document_id, self.base_version_id, self.restore_version_id):
            if value is not None:
                UUID(value)
        for relation in self.relations:
            UUID(relation.get("target_id", ""))
            if len(relation.get("label", "相关阅读")) > 300:
                raise ValueError("relation label is too long")
        return self


class KnowledgeService:
    def __init__(self, sessions):
        self.sessions = sessions

    def grant_direct(self, root_id: str) -> None:
        """仅供可信用户入口调用；不注册成 Agent 工具，不接受模型的自我授权。"""
        with self.sessions.begin() as session:
            session.execute(
                insert(TaskGrant)
                .values(root_id=root_id, mode="direct", actor="user")
                .on_conflict_do_update(index_elements=["root_id"], set_={"mode": "direct"})
            )

    def _citation_ids(self, session, body: str, root_id: str, inherited: set[str]) -> list[str]:
        ids = evidence_ids(body)
        for evidence_id in ids:
            try:
                UUID(evidence_id)
            except ValueError:
                raise ValueError("invalid evidence reference") from None
            evidence = session.get(Evidence, evidence_id)
            if evidence is None or (evidence.root_id != root_id and evidence_id not in inherited):
                raise ValueError(
                    "evidence must be read by this task or inherited from its base version"
                )
        render_citation_markers(body, ids)
        return sorted(ids)

    def _base(self, session, operation: DocumentChange) -> KnowledgeVersion | None:
        if operation.action == "create":
            return None
        base = session.get(KnowledgeVersion, operation.base_version_id)
        if base is None or base.document_id != operation.document_id:
            raise ValueError("base version does not belong to the document")
        return base

    def _changes(self, root_id: str, operations: list[dict]) -> list[DocumentChange]:
        if not root_id or not 1 <= len(operations) <= 20:
            raise ValueError("a draft requires root ID and 1..20 operations")
        changes = [DocumentChange.model_validate(item) for item in operations]
        for item in changes:
            if item.action == "create":
                item.document_id = item.document_id or new_id()
        if len({item.document_id for item in changes}) != len(changes):
            raise ValueError("one changeset cannot modify the same document twice")
        return changes

    def _requires_impact(self, changes: list[DocumentChange]) -> bool:
        """按操作组合识别结构变化，不能靠模型是否称其为合并/拆分来授权。

        拆分可保留原文并另建文章；合并也可更新现有目标并归档单一来源。
        两者都需要用户核对整份变更集，直接保存授权不能覆盖该确认。
        """
        archives = sum(item.action == "archive" for item in changes)
        creates = sum(item.action == "create" for item in changes)
        updates = sum(item.action == "update" for item in changes)
        return archives > 1 or bool(creates and (archives or updates)) or bool(archives and updates)

    def _require_read(self, session, version: KnowledgeVersion, root_id: str) -> None:
        """合并已读区间，全文已读才允许 Agent 修改；搜索摘要不登记全文证据。"""
        end = 0
        for evidence in session.scalars(
            select(Evidence)
            .where(Evidence.root_id == root_id, Evidence.knowledge_version_id == version.id)
            .order_by(Evidence.start_offset)
        ):
            if evidence.start_offset > end:
                break
            end = max(end, evidence.end_offset)
        if end < len(version.body):
            raise ValueError("read the entire base version before modifying it")

    def _validate_changes(
        self,
        session,
        root_id: str,
        changes: list[DocumentChange],
        require_read: bool = False,
        extra_evidence: set[str] | None = None,
    ) -> None:
        for item in changes:
            base = self._base(session, item)
            if base and item.action == "update":
                # 在固定基础版本上解析缺省字段，不从可能变化的“最新版本”补值。
                for name in ("summary", "category", "tags"):
                    if name not in item.model_fields_set:
                        value = getattr(base, name)
                        setattr(item, name, list(value) if isinstance(value, list) else value)
                if "relations" not in item.model_fields_set:
                    item.relations = self._relations(session, base.id)
            if base and require_read:
                self._require_read(session, base, root_id)
            if item.action == "restore":
                historical = session.get(KnowledgeVersion, item.restore_version_id)
                if historical is None or historical.document_id != item.document_id:
                    raise ValueError("historical version does not belong to the document")
                if require_read:
                    self._require_read(session, historical, root_id)
            inherited = (
                set(
                    session.scalars(
                        select(Citation.evidence_id).where(Citation.version_id == base.id)
                    )
                )
                if base
                else set()
            )
            if item.action in {"create", "update"}:
                verified_ids = self._citation_ids(
                    session, item.body, root_id, inherited | (extra_evidence or set())
                )
                item.body = render_citation_markers(item.body, verified_ids)

    def create_draft(
        self,
        root_id: str,
        operations: list[dict],
        *,
        proactive: bool = False,
        operation_key: str | None = None,
        require_prior_read: bool = False,
        guard: Callable | None = None,
        workspace_paths: dict | None = None,
        independently_saved: bool = False,
    ) -> dict:
        """草稿和幂等标识同事务保存；工具恢复重放不能产生第二份待审核内容。

        operation_key 由运行 ID 和真实 tool_call_id 构造；不由模型传入。
        请求指纹保存生成文档 ID 前的输入，重放与用户后续编辑不会相互混淆。
        """
        canonical = [
            DocumentChange.model_validate(item).model_dump(exclude_unset=True)
            for item in operations
        ]
        request_values = [root_id, proactive, canonical]
        if independently_saved:
            request_values.append({"independently_saved": True})
        fingerprint = hashlib.sha256(
            json.dumps(request_values, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        changes = self._changes(root_id, operations)
        if operation_key is not None and not 1 <= len(operation_key) <= 200:
            raise ValueError("invalid idempotency key")
        with self.sessions.begin() as session:
            from knowledgeops_agent.app.runs.control import lock_control

            lock_control(session)
            if guard:
                guard(session)
            if operation_key:
                # 相同键的并发重放串行；哈希碰撞最多让无关请求暂时互等。
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": operation_key},
                )
                existing = session.scalar(
                    select(ChangeSet).where(ChangeSet.operation_key == operation_key)
                )
                if existing:
                    expected = fingerprint
                    if existing.operation_format == 1:
                        # 兼容旧工具的同调用重放，但不赋予旧草稿新的发布语义。
                        old_input = [
                            DocumentChange.model_validate(item).model_dump() for item in operations
                        ]
                        expected = hashlib.sha256(
                            json.dumps(
                                [root_id, proactive, old_input] + ([{"independently_saved": True}] if independently_saved else []),
                                ensure_ascii=False, sort_keys=True
                            ).encode()
                        ).hexdigest()
                    if existing.request_fingerprint != expected:
                        raise ValueError("idempotency key belongs to another draft request")
                    return self._draft_view(session, existing)
            self._validate_changes(session, root_id, changes, require_prior_read)
            run = session.get(Run, root_id)
            draft = ChangeSet(
                root_id=root_id,
                retention_intent="saved" if independently_saved else "temporary" if run is not None else "legacy",
                learning_unit_id=run.learning_unit_id if run else None,
                publication_root_id=root_id,
                operations=[item.model_dump() for item in changes],
                validated_evidence_ids=sorted(
                    {identifier for item in changes for identifier in evidence_ids(item.body)}
                ),
                operation_format=2,
                proactive=proactive,
                operation_key=operation_key,
                request_fingerprint=fingerprint,
                requires_impact_confirmation=self._requires_impact(changes),
                workspace_paths=workspace_paths or {},
            )
            session.add(draft)
            session.flush()
            if draft.learning_unit_id:
                from knowledgeops_agent.persistence.models import LearningUnit

                unit = session.get(LearningUnit, draft.learning_unit_id)
                unit.draft_id, unit.status = draft.id, "drafting"
            self._save_revision(session, draft, root_id)
            return self._draft_view(session, draft)

    def _save_revision(self, session, draft: ChangeSet, actor_run_id: str | None) -> None:
        from knowledgeops_agent.app.lifecycle.references import replace_references

        replace_references(session, "draft", f"{draft.id}:{draft.revision}", draft.operations)
        content = json.dumps(draft.operations, ensure_ascii=False, sort_keys=True)
        session.execute(
            insert(ChangeSetRevision)
            .values(
                changeset_id=draft.id,
                revision=draft.revision,
                operation_format=draft.operation_format,
                operations=draft.operations,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                actor_run_id=actor_run_id,
            )
            .on_conflict_do_nothing()
        )

    def draft_revision(self, changeset_id: str, revision: int) -> dict:
        """读取已存在的不可变修订；缺失的旧历史不从现稿倒推。"""
        with self.sessions() as session:
            row = session.get(ChangeSetRevision, (changeset_id, revision))
            if row is None:
                raise KeyError("draft revision not preserved")
            return {
                "changeset_id": row.changeset_id,
                "revision": row.revision,
                "operations": row.operations,
                "content_hash": row.content_hash,
                "operation_format": row.operation_format,
            }

    def _draft_actor(self, session, draft: ChangeSet, actor_run_id: str) -> None:
        """只允许当前主任务读取同会话已经产生的草稿，不把知道ID当作访问范围。"""
        actor, origin = session.get(Run, actor_run_id), session.get(Run, draft.root_id)
        if (
            actor is None
            or actor.parent_id
            or origin is None
            or not actor.conversation_id
            or actor.conversation_id != origin.conversation_id
            or origin.created_at > actor.created_at
        ):
            raise PermissionError("changeset is outside this conversation/task scope")

    def draft_for_run(self, changeset_id: str, actor_run_id: str) -> dict:
        with self.sessions() as session:
            draft = session.get(ChangeSet, changeset_id)
            if draft is None:
                raise KeyError("changeset not found")
            self._draft_actor(session, draft, actor_run_id)
            return self._draft_view(session, draft)

    def _operation_replay(self, session, key: str | None, fingerprint: str) -> dict | None:
        if key is None:
            return None
        if not 1 <= len(key) <= 200:
            raise ValueError("invalid idempotency key")
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
        )
        receipt = session.get(OperationReceipt, key)
        if receipt:
            if receipt.request_hash != fingerprint:
                raise ValueError("idempotency key belongs to another operation")
            return receipt.result
        return None

    def _operation_result(self, session, key: str | None, fingerprint: str, result: dict) -> dict:
        if key:
            session.add(
                OperationReceipt(operation_key=key, request_hash=fingerprint, result=result)
            )
        return result

    def _draft_view(self, session, draft: ChangeSet) -> dict:
        before = {}
        after_versions = {}
        differences = []
        for item in draft.operations:
            if item.get("base_version_id"):
                version = session.get(KnowledgeVersion, item["base_version_id"])
                if version:
                    before[item["document_id"]] = self._version_view(version)
                    before[item["document_id"]]["relations"] = self._relations(session, version.id)
            old = before.get(item["document_id"], {}).get("body", "")
            after = item.get("body", "")
            preview = dict(item)
            if item["action"] == "archive":
                after = old
                preview = {**before.get(item["document_id"], {}), "archived": True}
            elif item["action"] == "restore":
                historical = session.get(KnowledgeVersion, item["restore_version_id"])
                after = historical.body if historical else ""
                preview = {
                    **(self._version_view(historical) if historical else {}),
                    "archived": False,
                }
            else:
                preview["archived"] = False
            after_versions[item["document_id"]] = preview
            differences.append(
                {
                    "document_id": item["document_id"],
                    "action": item["action"],
                    "diff": "".join(
                        difflib.unified_diff(
                            old.splitlines(keepends=True),
                            after.splitlines(keepends=True),
                            fromfile="原文",
                            tofile="变更后",
                            lineterm="\n",
                        )
                    ),
                }
            )
        return {
            "id": draft.id,
            "root_id": draft.root_id,
            "retention_intent": draft.retention_intent,
            "status": draft.status,
            "revision": draft.revision,
            "operation_format": draft.operation_format,
            "operations": draft.operations,
            "before": before,
            "after": after_versions,
            "differences": differences,
            "proactive": draft.proactive,
            "requires_impact_confirmation": draft.requires_impact_confirmation,
            "impact_confirmed": draft.impact_confirmed,
        }

    def get_draft(self, changeset_id: str) -> dict:
        with self.sessions() as session:
            draft = session.get(ChangeSet, changeset_id)
            if draft is None:
                raise KeyError("changeset not found")
            return self._draft_view(session, draft)

    def save_draft(self, changeset_id: str, *, expected_revision: int) -> dict:
        """真人独立保留草稿，不发布内容、不改变正文修订或扩大原授权。"""
        from knowledgeops_agent.app.runs.control import lock_control

        with self.sessions.begin() as session:
            lock_control(session)
            draft = session.get(ChangeSet, changeset_id)
            if draft is None:
                raise KeyError("changeset not found")
            if draft.revision != expected_revision:
                raise ValueError("草稿已变化，请刷新后再保存")
            run = session.get(Run, draft.root_id)
            if run and run.conversation_id:
                from knowledgeops_agent.sources.ownership import resolve_owner

                resolve_owner(session, conversation_id=run.conversation_id)
            draft.retention_intent = "saved"
            return self._draft_view(session, draft)

    def list_drafts(self, *, root_id: str | None = None) -> list[dict]:
        return self.drafts_page(root_id=root_id, limit=100)["items"]

    def drafts_page(
        self,
        *,
        root_id: str | None = None,
        conversation_id: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict:
        """会话筛选和状态筛选在SQL中执行，不能先取全局前100条再由前端过滤。"""
        query = select(ChangeSet)
        if root_id:
            query = query.where(
                (ChangeSet.root_id == root_id) | (ChangeSet.publication_root_id == root_id)
            )
        if conversation_id:
            query = query.join(Run, Run.id == ChangeSet.root_id).where(
                Run.conversation_id == conversation_id
            )
        if status:
            query = query.where(ChangeSet.status == status)
        with self.sessions() as session:
            rows, next_cursor = page_rows(session, query, ChangeSet, cursor=cursor, limit=limit)
            return {
                "items": [self._draft_view(session, draft) for draft in rows],
                "next_cursor": next_cursor,
            }

    def edit_draft(
        self,
        changeset_id: str,
        operations: list[dict],
        *,
        expected_revision: int,
        actor_run_id: str | None = None,
        operation_key: str | None = None,
        guard: Callable | None = None,
        workspace_paths: dict | None = None,
        require_prior_read: bool = True,
    ) -> dict:
        """编辑使原审批失效；正文基础版本和草稿编辑版本分别检查。"""
        fingerprint = hashlib.sha256(
            json.dumps(
                ["edit", changeset_id, expected_revision, actor_run_id, operations],
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self.sessions.begin() as session:
            from knowledgeops_agent.app.runs.control import lock_control

            lock_control(session)
            if guard:
                guard(session)
            replay = self._operation_replay(session, operation_key, fingerprint)
            if replay is not None:
                return replay
            draft = session.scalar(
                select(ChangeSet).where(ChangeSet.id == changeset_id).with_for_update()
            )
            if draft is None:
                raise KeyError("changeset not found")
            if actor_run_id:
                self._draft_actor(session, draft, actor_run_id)
            if draft.revision != expected_revision:
                raise ValueError("draft revision conflict")
            if draft.status not in {"draft", "approved"}:
                raise ValueError("changeset is no longer editable")
            changes = self._changes(draft.root_id, operations)
            inherited = {
                identifier
                for item in draft.operations
                for identifier in evidence_ids(item.get("body", ""))
            }
            self._validate_changes(
                session,
                actor_run_id or draft.root_id,
                changes,
                require_read=actor_run_id is not None and require_prior_read,
                extra_evidence=inherited,
            )
            legacy_update = draft.operation_format < 2 and any(
                item.action == "update" for item in changes
            )
            draft.operations = [item.model_dump() for item in changes]
            draft.publication_root_id = actor_run_id
            draft.validated_evidence_ids = sorted(
                {identifier for item in changes for identifier in evidence_ids(item.body)}
            )
            draft.operation_format = 2
            if workspace_paths is not None:
                draft.workspace_paths = workspace_paths
            draft.revision += 1
            draft.status, draft.approved_at, draft.impact_confirmed = "draft", None, False
            draft.requires_impact_confirmation = bool(
                draft.requires_impact_confirmation
                or legacy_update
                or self._requires_impact(changes)
            )
            self._save_revision(session, draft, actor_run_id)
            return self._operation_result(
                session, operation_key, fingerprint, self._draft_view(session, draft)
            )

    def reject(
        self,
        changeset_id: str,
        *,
        expected_revision: int,
        actor_run_id: str | None = None,
        operation_key: str | None = None,
        guard: Callable | None = None,
    ) -> dict:
        """用户拒绝优先于根任务的直接保存授权，拒绝后不会再自动发布此草稿。"""
        fingerprint = hashlib.sha256(
            json.dumps(["reject", changeset_id, expected_revision, actor_run_id]).encode()
        ).hexdigest()
        with self.sessions.begin() as session:
            from knowledgeops_agent.app.runs.control import lock_control

            lock_control(session)
            if guard:
                guard(session)
            replay = self._operation_replay(session, operation_key, fingerprint)
            if replay is not None:
                return replay
            draft = session.scalar(
                select(ChangeSet).where(ChangeSet.id == changeset_id).with_for_update()
            )
            if draft is None:
                raise KeyError("changeset not found")
            if actor_run_id:
                self._draft_actor(session, draft, actor_run_id)
            if draft.revision != expected_revision:
                raise ValueError("draft revision conflict")
            if draft.status not in {"draft", "approved", "rejected"}:
                raise ValueError("published changeset cannot be rejected")
            draft.status, draft.approved_at = "rejected", None
            return self._operation_result(
                session, operation_key, fingerprint, self._draft_view(session, draft)
            )

    def approve(
        self,
        changeset_id: str,
        *,
        confirm_impact: bool = False,
        expected_revision: int | None = None,
    ) -> dict:
        """保存用户对这份具体草稿的决定，不能用笼统批准覆盖另一份草稿。"""
        with self.sessions.begin() as session:
            from knowledgeops_agent.app.runs.control import lock_control

            lock_control(session)
            draft = session.scalar(
                select(ChangeSet).where(ChangeSet.id == changeset_id).with_for_update()
            )
            if draft is None:
                raise KeyError("changeset not found")
            self._approve_draft(draft, confirm_impact, expected_revision)
            return self._draft_view(session, draft)

    def _approve_draft(
        self, draft: ChangeSet, confirm_impact: bool, expected_revision: int | None
    ) -> None:
        """UI审批与聊天批准共用校验；调用者已锁草稿，聊天流程在发布事务内调用。"""
        if expected_revision is not None and draft.revision != expected_revision:
            raise ValueError("draft revision conflict")
        if draft.status not in {"draft", "approved"}:
            raise ValueError("changeset is no longer awaiting approval")
        if draft.requires_impact_confirmation and not confirm_impact:
            raise PermissionError("confirm the displayed merge/split/archive impact first")
        draft.status = "approved"
        draft.approved_at = utcnow()
        draft.impact_confirmed = confirm_impact

    def _check_publication_permission(
        self, session, draft: ChangeSet, actor_run_id: str | None = None
    ) -> None:
        if draft.status not in {"draft", "approved"}:
            raise PermissionError("changeset is not publishable")
        from knowledgeops_agent.learning.collaboration import publication_permission

        learning_grant = publication_permission(session, draft, actor_run_id)
        grant = (
            session.get(TaskGrant, draft.publication_root_id) if draft.publication_root_id else None
        )
        approved = draft.status == "approved"
        direct = (
            grant is not None
            and grant.mode == "direct"
            and not draft.proactive
            and (actor_run_id is None or actor_run_id == draft.publication_root_id)
        )
        if not approved and not direct and not learning_grant:
            raise PermissionError("user publication choice is required")
        if draft.requires_impact_confirmation and not draft.impact_confirmed:
            raise PermissionError("impact confirmation is required")

    def publish(self, changeset_id: str, **kwargs) -> dict:
        """与通用文件编辑共用锁；参数合同由 _publish_with_files 明确定义。"""
        from knowledgeops_agent.knowledge.files import workspace_lock

        with workspace_lock(self.sessions):
            return self._publish_with_files(changeset_id, **kwargs)

    def _publish_with_files(
        self,
        changeset_id: str,
        *,
        idempotency_key: str,
        expected_revision: int | None = None,
        guard: Callable | None = None,
        user_approval: bool = False,
        confirm_impact: bool = False,
        actor_run_id: str | None = None,
    ) -> dict:
        """先完成受控发布，再刷新可重建目录；导出失败不能谎报事务回滚。"""
        from knowledgeops_agent.knowledge.files import publication_files, refresh_working_files
        from knowledgeops_agent.persistence.workspace import sync_knowledge_projection

        # 比较草稿对应的工作文件，不能用一次旧发布重放覆盖后来尚未保存的编辑。
        expected_files = publication_files(self.sessions, changeset_id)
        result = self._publish_transaction(
            changeset_id,
            idempotency_key=idempotency_key,
            expected_revision=expected_revision,
            guard=guard,
            user_approval=user_approval,
            confirm_impact=confirm_impact,
            actor_run_id=actor_run_id,
        )
        if self.sessions.kw.get("info", {}).get("file_store") is not None:
            try:
                dirty = refresh_working_files(
                    self.sessions, result["document_ids"], expected_files=expected_files
                )
                status = sync_knowledge_projection(self.sessions)
                if dirty:
                    status = {
                        **status,
                        "pending_working_files": dirty,
                        "message": "正式版本已发布，较新的工作文件编辑已保留，请核对后同步",
                    }
            except (OSError, ValueError):
                # 正式不可变文件和 PG 回执已经成功。稍后重建 current 即可；
                # 不把此错误作为重做业务写入的理由，也不隐藏目录尚未同步。
                status = {"status": "pending", "message": "正文已发布，当前文件视图待重新导出"}
            return {**result, "workspace_export": status}
        return result

    def _publish_transaction(
        self,
        changeset_id: str,
        *,
        idempotency_key: str,
        expected_revision: int | None = None,
        guard: Callable | None = None,
        user_approval: bool = False,
        confirm_impact: bool = False,
        actor_run_id: str | None = None,
    ) -> dict:
        """原子提交变更及回执；任何异常让整个事务回滚。

        先锁草稿，再按文档 ID 排序锁目标，跨草稿并发更新不会互相覆盖。
        发布重放返回已有回执。基础版本过期必须重新读取、修订并重新授权，
        不能将模型的“继续执行”当作覆盖冲突的理由。
        """
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("valid idempotency key is required")
        with self.sessions.begin() as session:
            from knowledgeops_agent.app.runs.control import lock_control

            lock_control(session)
            if guard:
                guard(session)
            draft = session.scalar(
                select(ChangeSet).where(ChangeSet.id == changeset_id).with_for_update()
            )
            if draft is None:
                raise KeyError("changeset not found")
            if actor_run_id:
                self._draft_actor(session, draft, actor_run_id)
            if expected_revision is not None and draft.revision != expected_revision:
                raise ValueError("draft revision conflict")
            receipt = session.scalar(
                select(PublishReceipt).where(PublishReceipt.idempotency_key == idempotency_key)
            )
            # 先校验键归属，再按草稿返回已有回执。OR 查询可能先返回自己的
            # 草稿回执，掩盖调用者误用了另一份已发布草稿的键。
            if receipt is not None and receipt.changeset_id != changeset_id:
                raise ValueError("idempotency key belongs to another changeset")
            if receipt is None:
                receipt = session.scalar(
                    select(PublishReceipt).where(PublishReceipt.changeset_id == changeset_id)
                )
            if receipt is not None:
                return receipt.result
            if draft.operation_format < 2 and any(
                item.get("action") == "update" for item in draft.operations
            ):
                raise ValueError(
                    "legacy draft metadata intent is ambiguous: revise and confirm the displayed changes"
                )
            if user_approval:
                # 仅可信用户入口传入；Agent发布工具没有该参数。与正文同事务，
                # 取消、版本/引用失败均回滚批准，不能遗留一次未来可消费的权限。
                if expected_revision is None:
                    raise ValueError("chat approval requires an exact draft revision")
                self._approve_draft(draft, confirm_impact, expected_revision)
            self._check_publication_permission(session, draft, actor_run_id)
            changes = [DocumentChange.model_validate(item) for item in draft.operations]
            locked = {
                doc.id: doc
                for doc in session.scalars(
                    select(KnowledgeDocument)
                    .where(KnowledgeDocument.id.in_([item.document_id for item in changes]))
                    .order_by(KnowledgeDocument.id)
                    .with_for_update()
                )
            }
            for item in changes:
                doc = locked.get(item.document_id)
                if item.action == "create":
                    if doc is not None:
                        raise ValueError("document already exists")
                elif doc is None or doc.current_version_id != item.base_version_id:
                    raise ValueError("version conflict: reread current document before publishing")
            # 先创建全部目标 ID，允许同一合并/拆分变更中建立新文档之间的关系。
            for item in changes:
                if item.action == "create":
                    doc = KnowledgeDocument(id=item.document_id)
                    session.add(doc)
                    locked[doc.id] = doc
            session.flush()
            version_ids = []
            for item in changes:
                doc = locked[item.document_id]
                binding = (draft.workspace_paths or {}).get(doc.id)
                if binding:
                    workspace_path = binding["path"]
                    store = session.info.get("file_store")
                    if store is None:
                        raise ValueError("workspace publication requires file storage")
                    target = store.path(workspace_path)
                    if (
                        not target.is_relative_to(store.path("knowledge/documents"))
                        or target.suffix != ".md"
                    ):
                        raise ValueError("invalid knowledge workspace path")
                    doc.workspace_path = workspace_path
                base = self._base(session, item)
                body_from = base
                if item.action == "restore":
                    body_from = session.get(KnowledgeVersion, item.restore_version_id)
                    if body_from is None or body_from.document_id != doc.id:
                        raise ValueError("historical version does not belong to the document")
                if item.action in {"archive", "restore"}:
                    if body_from is None:
                        raise ValueError("archive/restore requires an existing version")
                    title, body = body_from.title, body_from.body
                    summary, category, tags = body_from.summary, body_from.category, body_from.tags
                    inherited = set(
                        session.scalars(
                            select(Citation.evidence_id).where(Citation.version_id == body_from.id)
                        )
                    )
                    relation_rows = session.execute(
                        select(KnowledgeRelation.target_id, KnowledgeRelation.label).where(
                            KnowledgeRelation.version_id == body_from.id
                        )
                    ).all()
                    relations = [
                        {"target_id": row.target_id, "label": row.label} for row in relation_rows
                    ]
                else:
                    title, body = item.title, item.body
                    summary, category, tags = item.summary, item.category, item.tags
                    inherited = (
                        set(
                            session.scalars(
                                select(Citation.evidence_id).where(Citation.version_id == base.id)
                            )
                        )
                        if base
                        else set()
                    )
                    relations = item.relations
                citation_ids = self._citation_ids(
                    session, body, draft.root_id, inherited | set(draft.validated_evidence_ids)
                )
                body = render_citation_markers(body, citation_ids)
                version = KnowledgeVersion(
                    document_id=doc.id,
                    number=base.number + 1 if base else 1,
                    title=title,
                    body=body,
                    summary=summary,
                    category=category,
                    tags=tags,
                    archived=item.action == "archive",
                    action=item.action,
                    root_id=draft.root_id,
                )
                session.add(version)
                session.flush()
                from knowledgeops_agent.app.lifecycle.references import replace_references

                replace_references(session, "asset", f"knowledge:{version.id}", body)
                # 文件先持久化，数据库再提交引用；正文不再同时维护两份。
                # 索引是派生数据，仍与当前版本指针在同一个 PG 事务中更新。
                store = session.info.get("file_store")
                if store is not None:
                    version.body_storage_key = f"knowledge/versions/{version.id}.md"
                    version.body_sha256 = store.put(version.body_storage_key, body, session=session, purpose="knowledge")
                    version._body = ""
                for evidence_id in citation_ids:
                    session.add(Citation(version_id=version.id, evidence_id=evidence_id))
                for relation in relations:
                    session.add(
                        KnowledgeRelation(
                            version_id=version.id,
                            target_id=relation["target_id"],
                            label=relation.get("label", "相关阅读"),
                        )
                    )
                doc.current_version_id = version.id
                session.execute(delete(SearchEntry).where(SearchEntry.document_id == doc.id))
                if not version.archived:
                    self._add_index(session, version)
                version_ids.append(version.id)
                session.flush()
            result = {
                "changeset_id": draft.id,
                "document_ids": [item.document_id for item in changes],
                "version_ids": version_ids,
                "outcome": "published",
            }
            if draft.learning_unit_id:
                from knowledgeops_agent.persistence.models import LearningUnit

                unit = session.get(LearningUnit, draft.learning_unit_id)
                unit.status, unit.stop_reason = "published", None
                unit.draft_id = draft.id
                unit.result_version_id = version_ids[0] if version_ids else None
                result["learning_unit_id"] = unit.id
                result["learning_publication"] = (
                    "human_approved" if draft.approved_at is not None else "reviewed_automatic"
                )
            session.add(
                PublishReceipt(
                    changeset_id=draft.id,
                    idempotency_key=idempotency_key,
                    result=result,
                )
            )
            draft.status = "published"
            session.flush()
            return result

    def _add_index(self, session, version: KnowledgeVersion) -> None:
        """索引和当前版本同事务；归档没有默认检索条目，但版本并未删除。"""
        metadata = " ".join([*version.tags, version.summary, version.category])
        session.add(
            SearchEntry(
                document_id=version.document_id,
                version_id=version.id,
                vector=weighted_vector(version.body, title=version.title, metadata=metadata),
                normalized_text="\n".join(
                    [
                        version.title,
                        version.summary,
                        version.category,
                        " ".join(version.tags),
                        version.body,
                    ]
                ).lower(),
            )
        )
        # 新片段外键挂在当前索引父记录下。替换/归档删除父记录时原子级联删除旧片段。
        session.flush()
        add_knowledge_chunks(session, version)

    def _version_view(self, version: KnowledgeVersion) -> dict:
        return {
            "id": version.document_id,
            "document_id": version.document_id,
            "version_id": version.id,
            "version": version.number,
            "title": version.title,
            "body": version.body,
            "summary": version.summary,
            "category": version.category,
            "tags": version.tags,
            "archived": version.archived,
            "action": version.action,
        }

    def _relations(self, session, version_id: str) -> list[dict]:
        """固定版本的关联信息供读取、缺省继承和预览共用。"""
        rows = session.execute(
            select(KnowledgeRelation.target_id, KnowledgeRelation.label)
            .where(KnowledgeRelation.version_id == version_id)
            .order_by(KnowledgeRelation.target_id)
        ).all()
        return [{"target_id": row.target_id, "label": row.label} for row in rows]

    def get_document(self, document_id: str, *, version_id: str | None = None) -> dict:
        with self.sessions() as session:
            doc = session.get(KnowledgeDocument, document_id)
            if doc is None:
                raise KeyError("document not found")
            version = session.get(KnowledgeVersion, version_id or doc.current_version_id)
            if version is None or version.document_id != doc.id:
                raise KeyError("document version not found")
            result = self._version_view(version)
            result["citation_ids"] = list(
                session.scalars(
                    select(Citation.evidence_id).where(Citation.version_id == version.id)
                )
            )
            result["relations"] = [
                {
                    "target_id": row.target_id,
                    "label": row.label,
                }
                for row in session.scalars(
                    select(KnowledgeRelation).where(KnowledgeRelation.version_id == version.id)
                )
            ]
            return result

    def list_documents(self, *, include_archived: bool = False) -> list[dict]:
        query = (
            select(KnowledgeVersion)
            .join(KnowledgeDocument, KnowledgeDocument.current_version_id == KnowledgeVersion.id)
            .order_by(KnowledgeDocument.created_at, KnowledgeDocument.id)
        )
        if not include_archived:
            query = query.where(KnowledgeVersion.archived.is_(False))
        with self.sessions() as session:
            return [self._version_view(version) for version in session.scalars(query)]

    def history(self, document_id: str) -> list[dict]:
        """版本不可变；前端按版本 ID 查看和选择恢复目标。"""
        with self.sessions() as session:
            return [
                self._version_view(version)
                for version in session.scalars(
                    select(KnowledgeVersion)
                    .where(KnowledgeVersion.document_id == document_id)
                    .order_by(KnowledgeVersion.number.desc())
                )
            ]

    def read_document(
        self,
        document_id: str,
        *,
        root_id: str,
        version_id: str | None = None,
        start: int = 0,
        length: int = 6000,
    ) -> dict:
        """按固定版本分页读取并登记已读区间，长正文不会被截断后冒充全文。"""
        with self.sessions.begin() as session:
            doc = session.get(KnowledgeDocument, document_id)
            if doc is None:
                raise KeyError("document not found")
            version = session.get(KnowledgeVersion, version_id or doc.current_version_id)
            if version is None or version.document_id != doc.id:
                raise KeyError("document version not found")
            if not 0 <= start < len(version.body) or not 1 <= length <= 12000:
                raise ValueError("invalid document range")
            end = min(len(version.body), start + length)
            evidence = Evidence(
                root_id=root_id,
                knowledge_version_id=version.id,
                start_offset=start,
                end_offset=end,
                quote=version.body[start:end],
            )
            session.add(evidence)
            session.flush()
            title = version.title.replace("[", "（").replace("]", "）")
            return {
                "id": evidence.id,
                "evidence_id": evidence.id,
                "document_id": doc.id,
                "knowledge_version_id": version.id,
                "version_id": version.id,
                "version": version.number,
                "is_current_version": version.id == doc.current_version_id,
                "archived": version.archived,
                "title": version.title,
                "text": evidence.quote,
                "summary": version.summary,
                "category": version.category,
                "tags": version.tags,
                "relations": self._relations(session, version.id),
                # 复用发布事务维护的固定版本引用关系，不从旧链接猜测失效。
                # 仅返回引用身份，不自动读取下游材料或登记为当前任务的新证据。
                "citation_ids": list(
                    session.scalars(
                        select(Citation.evidence_id)
                        .where(Citation.version_id == version.id)
                        .order_by(Citation.evidence_id)
                    )
                ),
                "start": start,
                "end": end,
                "total_length": len(version.body),
                "next_start": end if end < len(version.body) else None,
                "citation": f"[{title}](/api/evidence/{evidence.id})",
            }

    def rebuild_index(self) -> int:
        """运维重建派生索引；事务失败会保留旧索引，不修改正文或历史版本。"""
        with self.sessions.begin() as session:
            # 与发布相同文档行锁，防止重建覆盖同时发布的新版本索引。
            documents = list(
                session.scalars(
                    select(KnowledgeDocument).order_by(KnowledgeDocument.id).with_for_update()
                )
            )
            count = 0
            for doc in documents:
                version = session.get(KnowledgeVersion, doc.current_version_id)
                session.execute(delete(SearchEntry).where(SearchEntry.document_id == doc.id))
                if version and not version.archived:
                    self._add_index(session, version)
                    count += 1
            return count

    def export_markdown(self, *, include_archived: bool = False) -> bytes:
        """导出正文、关系与固定证据快照；实现单独维护，避免事务服务持续膨胀。"""
        from knowledgeops_agent.knowledge.export import export_markdown

        return export_markdown(self.sessions, include_archived=include_archived)

    def search(self, query: str, *, limit: int = 10) -> list[dict]:
        """先搜正文片段，再按文章去重；长文章不会因重复命中挤走其他候选。

        默认只查已发布的当前非归档版本。历史版本仍可按ID读取，检索片段不是
        已读证据；模型须根据返回的版本与偏移调用read_knowledge登记引用。
        """
        parsed = LexicalQuery.parse(query)
        if not parsed.terms:
            return []
        chunk = KnowledgeSearchChunk
        match, score = search_expressions(chunk, parsed, knowledge=True)
        candidates = (
            select(
                chunk.version_id,
                chunk.start,
                chunk.end,
                chunk.chunk_index,
                score.label("score"),
                func.row_number()
                .over(
                    partition_by=chunk.document_id,
                    order_by=(score.desc(), chunk.chunk_index),
                )
                .label("position"),
            )
            .join(KnowledgeDocument, KnowledgeDocument.id == chunk.document_id)
            .join(KnowledgeVersion, KnowledgeVersion.id == chunk.version_id)
            .where(
                match,
                KnowledgeDocument.current_version_id == chunk.version_id,
                KnowledgeVersion.archived.is_(False),
            )
            .subquery()
        )
        statement = (
            select(
                KnowledgeVersion,
                candidates.c.score,
                candidates.c.start,
                candidates.c.end,
                candidates.c.chunk_index,
            )
            .join(candidates, candidates.c.version_id == KnowledgeVersion.id)
            .where(candidates.c.position == 1)
            .order_by(candidates.c.score.desc(), KnowledgeVersion.id)
            .limit(max(1, min(limit, 30)))
        )
        with self.sessions() as session:
            results = []
            for version, score, start, end, chunk_index in session.execute(statement):
                passage = excerpt(version.body, start, end, parsed, width=700)
                results.append(
                    {
                        "document_id": version.document_id,
                        "version_id": version.id,
                        "version": version.number,
                        "title": version.title,
                        "score": float(score),
                        "snippet": passage.text,
                        "start": passage.start,
                        "end": passage.end,
                        "chunk_index": chunk_index,
                        "chunk_start": start,
                        "chunk_end": end,
                    }
                )
            return results
