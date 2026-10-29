"""业务数据模型。与费用账本分库，版本和证据不可原地覆盖。

current_version_id 只是当前指针；历史回答、引用和操作回执始终指向稳定 ID。
正文、索引、关系和回执由 KnowledgeService 在同一个事务里提交。
"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def new_id() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    identity_key: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(30))
    origin: Mapped[str | None] = mapped_column(Text)
    # 旧来源没有保存意图，不能从文件名或运行日志推断后自动回收。
    ownership_known: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssetReference(Base):
    """正文实际使用的资产边；记录存在本身不使目标获得永久保留。"""

    __tablename__ = "asset_references"
    __table_args__ = (Index("ix_asset_references_origin", "origin_node"),
                      Index("ix_asset_references_target", "target_node"))
    owner_kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    target_node: Mapped[str] = mapped_column(String(200), primary_key=True)
    origin_node: Mapped[str] = mapped_column(String(200))


class SourceUse(Base):
    """一份去重材料可以独立保存，也可以同时属于多个会话。"""

    __tablename__ = "source_uses"
    __table_args__ = (
        CheckConstraint("owner_kind IN ('library', 'conversation', 'run')"),
        Index("ix_source_uses_owner", "owner_kind", "owner_id"),
    )
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True)
    owner_kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceVersion(Base):
    __tablename__ = "source_versions"
    __table_args__ = (UniqueConstraint("source_id", "content_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    # content_hash 保留旧原件摘要语义；新网页用正文+解析器指纹去重，不改旧身份。
    content_hash: Mapped[str] = mapped_column(String(64))
    content_hash_kind: Mapped[str] = mapped_column(String(30), default="original", server_default="original")
    body_hash: Mapped[str | None] = mapped_column(String(64))
    body_format: Mapped[str | None] = mapped_column(String(30))
    original_hash: Mapped[str | None] = mapped_column(String(64))
    storage_key: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30), default="ready")
    parser: Mapped[str] = mapped_column(String(80), default="text-v1")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceSegment(Base):
    __tablename__ = "source_segments"
    __table_args__ = (
        UniqueConstraint("source_version_id", "position"),
        CheckConstraint("start_offset >= 0 AND end_offset >= start_offset"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_version_id: Mapped[str] = mapped_column(ForeignKey("source_versions.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    page: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(Text)
    start_offset: Mapped[int] = mapped_column(Integer, default=0)
    end_offset: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(String(30), default="text")
    status: Mapped[str] = mapped_column(String(30), default="ready")
    error: Mapped[str | None] = mapped_column(Text)


class DeliveredFile(Base):
    """用户已收到的独立文件资产；来源Run仅用于追溯，不授予级联删除权。"""

    __tablename__ = "delivered_files"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    root_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(Text)
    source_path: Mapped[str] = mapped_column(Text)
    storage_key: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    mime_type: Mapped[str] = mapped_column(String(120))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_path: Mapped[str | None] = mapped_column(Text, unique=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # use_alter 明确循环外键的建表顺序；新建文档可在同一事务先插入空指针再设置。
    current_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_versions.id", use_alter=True, name="fk_current_knowledge_version")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeVersion(Base):
    __tablename__ = "knowledge_versions"
    __table_args__ = (UniqueConstraint("document_id", "number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500))
    _body: Mapped[str] = mapped_column("body", Text)
    body_storage_key: Mapped[str | None] = mapped_column(Text)
    body_sha256: Mapped[str | None] = mapped_column(String(64))

    @property
    def body(self) -> str:
        """新版本从工作目录读取；旧内联数据仅用于无损迁移兼容。"""
        from knowledgeops_agent.persistence.files import stored_text

        return stored_text(self, self.body_storage_key, self.body_sha256, self._body)

    @body.setter
    def body(self, value: str) -> None:
        self._body = value

    summary: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(200), default="")
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    action: Mapped[str] = mapped_column(String(30))
    root_id: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint(
            "(source_segment_id IS NOT NULL)::int + (knowledge_version_id IS NOT NULL)::int = 1"
        ),
        CheckConstraint("start_offset >= 0 AND end_offset >= start_offset"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    root_id: Mapped[str] = mapped_column(String(120), index=True)
    source_segment_id: Mapped[str | None] = mapped_column(ForeignKey("source_segments.id"))
    knowledge_version_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_versions.id"))
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    quote: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Citation(Base):
    __tablename__ = "citations"
    version_id: Mapped[str] = mapped_column(ForeignKey("knowledge_versions.id"), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence.id"), primary_key=True)


class KnowledgeRelation(Base):
    __tablename__ = "knowledge_relations"
    version_id: Mapped[str] = mapped_column(ForeignKey("knowledge_versions.id"), primary_key=True)
    target_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id"), primary_key=True)
    label: Mapped[str] = mapped_column(String(300), default="相关阅读")


class SearchEntry(Base):
    __tablename__ = "search_entries"
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id"), primary_key=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("knowledge_versions.id"), unique=True)
    vector: Mapped[str] = mapped_column(TSVECTOR)
    normalized_text: Mapped[str] = mapped_column(Text)
    __table_args__ = (Index("ix_search_entries_vector", "vector", postgresql_using="gin"),)


class KnowledgeSearchChunk(Base):
    """当前正文的可重建段落索引；删除旧索引时级联，原版本始终保留。"""

    __tablename__ = "knowledge_search_chunks"
    document_id: Mapped[str] = mapped_column(
        ForeignKey("search_entries.document_id", ondelete="CASCADE"), primary_key=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("knowledge_versions.id"), index=True)
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)
    body_tokens: Mapped[str] = mapped_column(Text)
    title_tokens: Mapped[str] = mapped_column(Text)
    metadata_tokens: Mapped[str] = mapped_column(Text)
    __table_args__ = (
        Index(
            "ix_knowledge_chunks_bm25",
            "document_id",
            "version_id",
            "chunk_index",
            text("(body_tokens::pdb.whitespace)"),
            text("(title_tokens::pdb.whitespace)"),
            text("(metadata_tokens::pdb.whitespace)"),
            postgresql_using="paradedb",
        ),
    )


class TaskGrant(Base):
    """可信用户选择，与模型可编辑的 State/摘要分开保存。"""

    __tablename__ = "task_grants"
    root_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    mode: Mapped[str] = mapped_column(String(20))
    actor: Mapped[str] = mapped_column(String(40), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChangeSet(Base):
    __tablename__ = "changesets"
    workspace_paths: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (UniqueConstraint("operation_key", name="uq_changesets_operation_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    learning_unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_units.id", use_alter=True, name="fk_changeset_learning_unit"),
        index=True,
    )
    root_id: Mapped[str] = mapped_column(String(120), index=True)
    operations: Mapped[list] = mapped_column(JSONB)
    # 草稿归属不变，自动发布许可随当前编辑任务更新；不能继承旧任务的direct。
    publication_root_id: Mapped[str | None] = mapped_column(
        String(120), default=lambda context: context.get_current_parameters().get("root_id")
    )
    validated_evidence_ids: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    # 旧草稿已丢失“未提供/明确清空”的差别；不能在升级后猜测原用户意图。
    operation_format: Mapped[int] = mapped_column(Integer, default=2, server_default="1")
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    operation_key: Mapped[str | None] = mapped_column(String(200))
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="draft")
    retention_intent: Mapped[str] = mapped_column(String(20), default="legacy", server_default="legacy")
    proactive: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_impact_confirmation: Mapped[bool] = mapped_column(Boolean, default=False)
    impact_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChangeSetRevision(Base):
    """正文修订快照不可变，供历史审查和辅助模型固定材料引用。"""

    __tablename__ = "changeset_revisions"
    changeset_id: Mapped[str] = mapped_column(ForeignKey("changesets.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_format: Mapped[int] = mapped_column(Integer)
    operations: Mapped[list] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    actor_run_id: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OperationReceipt(Base):
    """非发布业务动作的幂等回执；与实际修订/拒绝同事务，保存原返回。"""

    __tablename__ = "operation_receipts"
    operation_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PublishReceipt(Base):
    """和正文同事务提交，工具重放可读取回执而不重复创建版本。"""

    __tablename__ = "publish_receipts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    changeset_id: Mapped[str] = mapped_column(ForeignKey("changesets.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Conversation(Base):
    """用户可见会话；不把模型内部工具历史直接当作用户聊天。"""

    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(300), default="新对话")
    creation_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    creation_fingerprint: Mapped[str | None] = mapped_column(String(64))
    activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    messages_revision: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    # 先封闭读写再跨存储清理；墓碑不保存已删除聊天正文。
    deletion_state: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runtime_settings: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    runtime_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    user_sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    execution_sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    latest_execution_sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    selected_learning_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_tasks.id", use_alter=True, name="fk_selected_learning_task")
    )
    latest_root_sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    latest_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("continuation_snapshots.id", use_alter=True, name="fk_latest_continuation")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConversationIndexState(Base):
    """会话目录的展示修订；只使旧游标失效，不成为聊天内容的保留依赖。"""

    __tablename__ = "conversation_index_state"
    __table_args__ = (CheckConstraint("id = 1"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    revision: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")


class ExecutionRule(Base):
    """可信用户配置的命令规则；软删除保留审批时使用的规则身份。"""

    __tablename__ = "execution_rules"
    __table_args__ = (
        CheckConstraint("effect IN ('allow','ask','deny')"),
        CheckConstraint("match IN ('exact','prefix')"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace: Mapped[str] = mapped_column(Text)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    cwd: Mapped[str] = mapped_column(Text)
    argv: Mapped[list] = mapped_column(JSONB)
    match: Mapped[str] = mapped_column(String(20))
    effect: Mapped[str] = mapped_column(String(20))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    source_action_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExecutionAction(Base):
    """具体工具调用的真人审批；批次/顺序固定，恢复时不重新按新策略排序。"""

    __tablename__ = "execution_actions"
    __table_args__ = (UniqueConstraint("run_id", "tool_call_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    root_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    tool_call_id: Mapped[str] = mapped_column(String(200))
    tool_name: Mapped[str] = mapped_column(String(200))
    arguments: Mapped[dict] = mapped_column(JSONB)
    fingerprint: Mapped[str] = mapped_column(String(64))
    workspace: Mapped[str] = mapped_column(Text)
    cwd: Mapped[str] = mapped_column(Text)
    authority_epoch: Mapped[int] = mapped_column(Integer)
    batch_key: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer)
    origin: Mapped[str] = mapped_column(String(20))
    interrupt_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    decision: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CommandExecution(Base):
    """副作用边界的回执。running 无结果只能等待核对，不能假定命令没发生。"""

    __tablename__ = "command_executions"
    __table_args__ = (UniqueConstraint("run_id", "tool_call_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    root_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    tool_call_id: Mapped[str] = mapped_column(String(200))
    fingerprint: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict] = mapped_column(JSONB)
    authority_epoch: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    result: Mapped[dict | None] = mapped_column(JSONB)
    history: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ToolBudgetReservation(Base):
    """只记录实际启动尝试的扣额身份；等待/恢复不会再扣相同一次尝试。"""

    __tablename__ = "tool_budget_reservations"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    call_key: Mapped[str] = mapped_column(String(240), primary_key=True)
    tool_name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    """持久任务兼运行记录；根任务入队，子任务由已领取的主运行创建。

    状态和预算属于程序控制数据，不接受模型工具直接改写。恢复保留 ID、
    已消费计数及 checkpoint_id；worker 身份防止旧执行者写回新一轮结果。
    """

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','interrupted','completed','cancelled','failed','budget_exhausted','paused','waiting_input','waiting_children')"
        ),
        CheckConstraint("tool_calls >= 0 AND searches >= 0 AND active_ms >= 0"),
        Index("ix_runs_claim", "status", "created_at"),
        UniqueConstraint(
            "conversation_id", "conversation_sequence", name="uq_conversation_run_sequence"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    root_id: Mapped[str] = mapped_column(String(36), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"))
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    learning_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_tasks.id"), index=True
    )
    learning_unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("learning_units.id"), index=True
    )
    origin: Mapped[str] = mapped_column(String(30), default="human", server_default="human")
    checkpoint_id: Mapped[str] = mapped_column(String(36), unique=True)
    # 旧运行没有可证明的资源清单；升级保留1，新运行显式使用2。
    format_version: Mapped[int] = mapped_column(Integer, default=2, server_default="1")
    conversation_sequence: Mapped[int | None] = mapped_column(Integer)
    execution_sequence: Mapped[int | None] = mapped_column(Integer)
    learning_accounted_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    continuation_bound: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    continuation_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("continuation_snapshots.id", use_alter=True, name="fk_run_continuation")
    )
    authority_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    applied_authority_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    request_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    # HTTP原始请求的规范摘要；先查已接受身份，再读取可能已变化的设置/材料。
    submission_fingerprint: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(30), default="agent")
    task: Mapped[str] = mapped_column(Text)
    input_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    budgets: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    outcome: Mapped[dict] = mapped_column(JSONB, default=dict)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    worker_id: Mapped[str | None] = mapped_column(String(120))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    searches: Mapped[int] = mapped_column(Integer, default=0)
    active_ms: Mapped[int] = mapped_column(Integer, default=0)
    active_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_sequence: Mapped[int] = mapped_column(Integer, default=0)
    # 展示结果的单调版本与诊断事件序号分开；已读不会修改执行或恢复状态。
    result_version: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    result_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 独立于执行 status；过期不伪造任务成功，也不能再次恢复已清理的位置。
    lifecycle_state: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    user_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_adopted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_eligible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    details_summary: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))


class ApplicationActivityState(Base):
    """跨页面观察时钟；事件和已读推进同事务提交，不依赖本机时间。"""

    __tablename__ = "application_activity_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    revision: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    __table_args__ = (CheckConstraint("id = 1"),)


class RunReadReceipt(Base):
    """当前单用户应用的独立已读位置；不持有正文，不是生命周期保留根。"""

    __tablename__ = "run_read_receipts"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (CheckConstraint("version >= 0"),)


class RunControl(Base):
    """可接续控制事实与普通取消分开；旧运行没有记录时按未暂停处理。"""

    __tablename__ = "run_controls"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    wait_spec: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FileOperation(Base):
    """通用文件修改的执行意图；正文在工作区不可变文件中，PG仅存引用。"""

    __tablename__ = "file_operations"
    __table_args__ = (UniqueConstraint("run_id", "tool_call_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    root_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    tool_call_id: Mapped[str] = mapped_column(String(200))
    fingerprint: Mapped[str] = mapped_column(String(64))
    workspace: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text)
    operation: Mapped[str] = mapped_column(String(30))
    authority_epoch: Mapped[int] = mapped_column(Integer)
    before_hash: Mapped[str | None] = mapped_column(String(64))
    after_hash: Mapped[str | None] = mapped_column(String(64))
    content_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30))
    result: Mapped[dict | None] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunQuestion(Base):
    """需要真人回答的信息，不作为权限批准；问题身份跨恢复保持不变。"""

    __tablename__ = "run_questions"
    __table_args__ = (UniqueConstraint("run_id", "tool_call_id", "round"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    root_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    tool_call_id: Mapped[str] = mapped_column(String(200))
    round: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(30))
    authority_epoch: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    request: Mapped[dict] = mapped_column(JSONB)
    answer: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    interrupt_id: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MCPContinuation(Base):
    """不透明的协议续接状态仅供客户端读取，不进入模型背景或普通事件。"""

    __tablename__ = "mcp_continuations"
    __table_args__ = (UniqueConstraint("mcp_call_id", "round"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    mcp_call_id: Mapped[str] = mapped_column(ForeignKey("mcp_calls.id"), index=True)
    question_id: Mapped[str] = mapped_column(ForeignKey("run_questions.id"))
    round: Mapped[int] = mapped_column(Integer)
    request_state: Mapped[dict] = mapped_column(JSONB)
    input_responses: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(30), default="waiting")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChildInstruction(Base):
    """主 Agent 与真人追加指令保留来源，在子图安全点应用。"""

    __tablename__ = "child_instructions"
    __table_args__ = (UniqueConstraint("run_id", "request_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    root_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(240))
    origin: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    authority_epoch: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChildNotification(Base):
    """子结果通知去重，消费位置进入父图检查点而不提前丢弃通知。"""

    __tablename__ = "child_notifications"
    __table_args__ = (UniqueConstraint("run_id", "generation", "event_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    root_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    event_key: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSONB)
    # 仅在父图消费位置已持久化后确认；收到网络通知并不等于已消费。
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelStep(Base):
    """图步骤与网关回执的稳定关联；请求快照用于提交窗口中的准确重放。"""

    __tablename__ = "model_steps"
    __table_args__ = (UniqueConstraint("run_id", "step_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    step_key: Mapped[str] = mapped_column(String(200))
    call_id: Mapped[str] = mapped_column(String(36), unique=True)
    authority_epoch: Mapped[int] = mapped_column(Integer)
    request_hash: Mapped[str] = mapped_column(String(64))
    request_snapshot: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (CheckConstraint("role IN ('user','assistant')"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    # 供应商协议的原消息，仅供下一轮模型重放；不得进入 UI 或全文索引。
    protocol_payload: Mapped[dict | None] = mapped_column(JSONB)
    user_sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MessageDelivery(Base):
    """明确选择的引导投递；消息原件保留，不能同时又作为独立排队任务执行。"""

    __tablename__ = "message_deliveries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    message_id: Mapped[str] = mapped_column(ForeignKey("chat_messages.id"), unique=True)
    source_run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), unique=True)
    target_root_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    target_generation: Mapped[int] = mapped_column(Integer)
    authority_epoch: Mapped[int] = mapped_column(Integer)
    user_sequence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunEvent(Base):
    """事务内分配递增序号；SSE 断线重连按序号补读，事件不只留在内存。"""

    __tablename__ = "run_events"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(80))
    data: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunDispatch(Base):
    """事务外盒：与任务同时提交，网络投递失败不会遗失待执行的任务。

    同一次恢复有唯一代次；反复发送只是在通知 worker，不等于重复获得
    执行授权。消费时还要检查对应运行的状态和当前代次。
    """

    __tablename__ = "run_dispatches"
    __table_args__ = (UniqueConstraint("run_id", "generation", name="uq_dispatch_generation"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    last_error: Mapped[str | None] = mapped_column(String(200))


class RunArtifact(Base):
    """不可变运行材料：完整工具输出和被压缩的上下文，不进入知识库检索。"""

    __tablename__ = "run_artifacts"
    __table_args__ = (
        UniqueConstraint("run_id", "kind", "content_hash", name="uq_run_artifact_content"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    content_hash: Mapped[str] = mapped_column(String(64))
    _content: Mapped[str] = mapped_column("content", Text)
    content_storage_key: Mapped[str | None] = mapped_column(Text)
    # 正式引用已转成固定来源后，旧产物身份只保留别名，不再次成为保留根。
    promoted_version_id: Mapped[str | None] = mapped_column(ForeignKey("source_versions.id", ondelete="SET NULL"), index=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def content(self) -> str:
        if self.promoted_version_id:
            from sqlalchemy.orm import object_session

            from knowledgeops_agent.sources.service import SourceService

            session = object_session(self)
            version = session.get(SourceVersion, self.promoted_version_id) if session is not None else None
            if version is None:
                raise ValueError("promoted material source is unavailable")
            return SourceService._verified_body(session, version)[1]
        from knowledgeops_agent.persistence.files import stored_text

        return stored_text(self, self.content_storage_key, self.content_hash, self._content)

    @content.setter
    def content(self, value: str) -> None:
        self._content = value


class TaskHistoryRecord(Base):
    """可见历史的不可变检索副本；scope及来源ID共同定位，隐藏推理不进入索引。"""

    __tablename__ = "task_history_records"
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(33), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    search_terms: Mapped[list[str]] = mapped_column(ARRAY(Text))
    __table_args__ = (Index("ix_task_history_terms", "search_terms", postgresql_using="gin"),)


class TaskHistoryChunk(Base):
    """历史消息的派生片段；可读性仍取决于检查点批次而非该表是否存在。"""

    __tablename__ = "task_history_chunks"
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(33), primary_key=True)
    chunk_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)
    body_tokens: Mapped[str] = mapped_column(Text)
    __table_args__ = (
        ForeignKeyConstraint(
            ["scope", "source_id"],
            ["task_history_records.scope", "task_history_records.source_id"],
            ondelete="CASCADE",
        ),
        Index(
            "ix_history_chunks_bm25",
            "scope",
            "source_id",
            "chunk_index",
            text("(body_tokens::pdb.whitespace)"),
            postgresql_using="paradedb",
        ),
    )


class TaskHistoryBatch(Base):
    """可读性由检查点中的批次ID决定；数据库里存在不等于任何运行都能读取。"""

    __tablename__ = "task_history_batches"
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    source_ids: Mapped[list[str]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ContinuationSnapshot(Base):
    """新run继承的背景快照，与恢复执行位置的LangGraph checkpoint分开。"""

    __tablename__ = "continuation_snapshots"
    __table_args__ = (UniqueConstraint("run_id", "generation", name="uq_continuation_generation"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    parent_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("continuation_snapshots.id"))
    covered_sequence: Mapped[int] = mapped_column(Integer)
    format_version: Mapped[int] = mapped_column(Integer, default=2)
    content: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunResourceSnapshot(Base):
    """首次执行冻结的可序列化资源；仅保存正文/身份，不含连接对象和密钥。"""

    __tablename__ = "run_resource_snapshots"
    root_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    content: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunSkillUse(Base):
    """实际文件读取的最小事实；与可到期清理的事件分开，随运行详情回收。"""

    __tablename__ = "run_skill_uses"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    resource: Mapped[str] = mapped_column(Text, primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SkillPackageState(Base):
    """当前包身份及移除标记；正文在项目目录，历史任务自持完整冻结字节。"""

    __tablename__ = "skill_packages"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64))
    removed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExtensionOperation(Base):
    """扩展管理操作事实；不保存包文件或明文密钥，后台租约不等于执行许可。"""

    __tablename__ = "extension_operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    target: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    expected_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parameters: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class ResourceRevocation(Base):
    """撤回特定版本不篡改历史快照；每次相关调用检查，而非只在装配时判断。"""

    __tablename__ = "resource_revocations"
    kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    resource: Mapped[str] = mapped_column(Text, primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MemoryCategoryDefinition(Base):
    """用户可维护的展示类别；与记忆内部 category 语义及权限完全分开。"""

    __tablename__ = "memory_categories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    name_key: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    origin: Mapped[str] = mapped_column(String(20), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        Index("uq_memory_category_active_name", "name_key", unique=True,
              postgresql_where=text("NOT deleted")),
    )


class Preference(Base):
    """通用长期记忆的当前指针；沿用表名和ID，旧偏好属于preference类别。"""

    __tablename__ = "preferences"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(20))
    topic: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    category: Mapped[str] = mapped_column(
        String(30), default="preference", server_default="preference"
    )
    display_category_id: Mapped[str | None] = mapped_column(ForeignKey("memory_categories.id"))
    # 只保存分类进度/来源与短错误码；任务正文、请求和恢复事实沿用 Run。
    classification: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    confidence: Mapped[float] = mapped_column(Float, default=1.0, server_default="1")
    revocation_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_message_id: Mapped[str | None] = mapped_column(ForeignKey("chat_messages.id"))
    source: Mapped[dict] = mapped_column(JSONB, default=dict)
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("preferences.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MemoryEvidence(Base):
    """真实用户证据关联；重复提取不增加证据，人工修订不虚构message_id。"""

    __tablename__ = "memory_evidence"
    __table_args__ = (
        UniqueConstraint("preference_id", "message_id", name="uq_memory_evidence_message"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    preference_id: Mapped[str] = mapped_column(ForeignKey("preferences.id"), index=True)
    message_id: Mapped[str | None] = mapped_column(ForeignKey("chat_messages.id"))
    kind: Mapped[str] = mapped_column(String(30))
    quote: Mapped[str] = mapped_column(Text, default="")
    context_key: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MemorySearchChunk(Base):
    """每修订的可重建索引；旧Run仍可查冻结版本，撤回由当前指针拦截。"""

    __tablename__ = "memory_search_chunks"
    preference_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    chunk_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    body_tokens: Mapped[str] = mapped_column(Text)
    __table_args__ = (
        ForeignKeyConstraint(
            ["preference_id", "revision"],
            ["preference_revisions.preference_id", "preference_revisions.revision"],
        ),
        Index(
            "ix_memory_chunks_bm25",
            "preference_id",
            "revision",
            "chunk_index",
            text("(body_tokens::pdb.whitespace)"),
            postgresql_using="paradedb",
        ),
    )


class PreferenceRevision(Base):
    """内容、状态和来源共同版本化，支持用户审查每次自动或手动变更。"""

    __tablename__ = "preference_revisions"
    preference_id: Mapped[str] = mapped_column(ForeignKey("preferences.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PreferenceExtraction(Base):
    """逐真人消息的解释回执；撤回之后重复解释不能复活旧偏好。"""

    __tablename__ = "preference_extractions"
    message_id: Mapped[str] = mapped_column(ForeignKey("chat_messages.id"), primary_key=True)
    extractor_version: Mapped[str] = mapped_column(String(40), primary_key=True)
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MCPInstallation(Base):
    """应用拥有的本地安装身份；程序/锁文件在项目内，删除只依据登记及使用锁。"""

    __tablename__ = "mcp_installations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    connection_id: Mapped[str] = mapped_column(String(36), index=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("extension_operations.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="preparing", index=True)
    request: Mapped[dict] = mapped_column(JSONB)
    manifest: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MCPConnection(Base):
    """连接当前指针；授权代次单调增加，禁用再启用不复活旧工具绑定。"""

    __tablename__ = "mcp_connections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    authorization_epoch: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict] = mapped_column(JSONB)
    tools: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MCPConnectionRevision(Base):
    """每次配置、发现及许可变更保留原版，恢复不读取可变的最新配置冒充旧版。"""

    __tablename__ = "mcp_connection_revisions"
    connection_id: Mapped[str] = mapped_column(ForeignKey("mcp_connections.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MCPCall(Base):
    """调用准备、用户决定、外部结果与材料回执；未知结果没有自动重发路径。"""

    __tablename__ = "mcp_calls"
    __table_args__ = (UniqueConstraint("run_id", "tool_call_id", name="uq_mcp_run_call"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("mcp_connections.id"))
    connection_revision: Mapped[int] = mapped_column(Integer)
    tool_call_id: Mapped[str] = mapped_column(String(200))
    tool_name: Mapped[str] = mapped_column(String(200))
    input_hash: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(30))
    authority_epoch: Mapped[int] = mapped_column(Integer)
    decision: Mapped[dict | None] = mapped_column(JSONB)
    response: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvaluationReport(Base):
    """一次固定评估的配置；完成评估不代表应用验收通过。"""

    __tablename__ = "evaluation_reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    configuration: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(30), default="running")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvaluationResult(Base):
    """单次案例的不可覆盖证据；审读意见单独保存，不能改写失败事实。"""

    __tablename__ = "evaluation_results"
    report_id: Mapped[str] = mapped_column(ForeignKey("evaluation_reports.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB)
    review: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def make_sessions(database_url: str, *, workspace_root=None):
    """每个业务操作自行持有事务；客户端连接池不写入 Agent 检查点。"""
    if not database_url:
        raise ValueError("DATABASE_URL is required")
    engine = create_engine(database_url, pool_pre_ping=True)
    from knowledgeops_agent.persistence.files import FileStore

    store = FileStore(workspace_root) if workspace_root else None
    return sessionmaker(engine, expire_on_commit=False, info={"file_store": store})


class SubagentDefinition(Base):
    """用户角色当前指针；稳定名称与墓碑保留历史，撤销代次不会回退。"""

    __tablename__ = "subagent_definitions"
    __table_args__ = (UniqueConstraint("name", name="uq_subagent_definition_name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(500))
    system_prompt: Mapped[str] = mapped_column(Text)
    tools: Mapped[list] = mapped_column(JSONB, default=list)
    # null表示0014旧行，读取时保持原修订形状；新配置保存明确的展开结果。
    tool_groups: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    resolved_tools: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    skills: Mapped[list] = mapped_column(JSONB, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    authorization_epoch: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SubagentDefinitionRevision(Base):
    """配置的不可变修订；冻结绑定必须与这份完整内容匹配。"""

    __tablename__ = "subagent_definition_revisions"
    definition_id: Mapped[str] = mapped_column(
        ForeignKey("subagent_definitions.id"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ToolGroup(Base):
    """可编辑本地工具组；稳定身份与删除墓碑不能因默认目录回退而复活。"""

    __tablename__ = "tool_groups"
    id: Mapped[str] = mapped_column(String(100), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(500), default="")
    tool_ids: Mapped[list] = mapped_column(JSONB, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ToolGroupRevision(Base):
    """工具组历史用于核对配置变更；执行许可使用原策略和角色的冻结展开结果。"""

    __tablename__ = "tool_group_revisions"
    group_id: Mapped[str] = mapped_column(ForeignKey("tool_groups.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ToolPolicy(Base):
    """本地/中间件工具策略的当前指针；MCP与角色状态仍由原表管理。"""

    __tablename__ = "tool_policies"
    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSONB)


class ToolPolicyRevision(Base):
    """完整不可变策略与逐工具撤回代次，供恢复核对而非重新推算旧许可。"""

    __tablename__ = "tool_policy_revisions"
    policy_id: Mapped[str] = mapped_column(ForeignKey("tool_policies.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LearningTask(Base):
    """业务目标独立持久化；不拥有另一套聊天历史或LangGraph检查点。"""

    __tablename__ = "learning_tasks"
    advance_sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(200), unique=True)
    creation_input: Mapped[dict] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    requirements_revision: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(300))
    goal: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text, default="")
    topic: Mapped[str] = mapped_column(String(200), default="")
    configuration: Mapped[dict] = mapped_column(JSONB, default=dict)
    mode: Mapped[str] = mapped_column(String(30), default="interactive")
    status: Mapped[str] = mapped_column(String(30), default="draft")
    confirmed_requirements_revision: Mapped[int | None] = mapped_column(Integer)
    auto_publish: Mapped[bool] = mapped_column(Boolean, default=False)
    authority_epoch: Mapped[int] = mapped_column(Integer, default=0)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("mode IN ('interactive','delegated')"),
        CheckConstraint(
            "status IN ('draft','active','paused','waiting','completed','partial','cancelled')"
        ),
    )


class LearningTaskRevision(Base):
    __tablename__ = "learning_task_revisions"
    task_id: Mapped[str] = mapped_column(ForeignKey("learning_tasks.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LearningUnit(Base):
    """有限内容单元及其累计进度；新建Run不能清零这些计数。"""

    __tablename__ = "learning_units"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("learning_tasks.id"), index=True)
    key: Mapped[str] = mapped_column(String(80))
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    goal: Mapped[str] = mapped_column(Text)
    dependencies: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    budgets: Mapped[dict] = mapped_column(JSONB)
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    searches: Mapped[int] = mapped_column(Integer, default=0)
    active_ms: Mapped[int] = mapped_column(Integer, default=0)
    reader_rounds: Mapped[int] = mapped_column(Integer, default=0)
    review_rounds: Mapped[int] = mapped_column(Integer, default=0)
    draft_id: Mapped[str | None] = mapped_column(ForeignKey("changesets.id"))
    result_version_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_versions.id"))
    stop_reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("task_id", "key"),
        CheckConstraint(
            "model_calls >= 0 AND tool_calls >= 0 AND searches >= 0 AND active_ms >= 0"
        ),
    )


class LearningCollaboration(Base):
    """固定草稿的真实子运行与完整阅读证据；主模型不能提交通过结论。"""

    __tablename__ = "learning_collaborations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("learning_tasks.id"), index=True)
    unit_id: Mapped[str] = mapped_column(ForeignKey("learning_units.id"), index=True)
    root_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    child_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), unique=True)
    role: Mapped[str] = mapped_column(String(20))
    operation_key: Mapped[str] = mapped_column(String(200), unique=True)
    requirements_revision: Mapped[int] = mapped_column(Integer)
    draft_id: Mapped[str] = mapped_column(ForeignKey("changesets.id"))
    draft_revision: Mapped[int] = mapped_column(Integer)
    draft_hash: Mapped[str] = mapped_column(String(64))
    configuration_hash: Mapped[str] = mapped_column(String(64))
    input_manifest: Mapped[dict] = mapped_column(JSONB)
    storage_key: Mapped[str] = mapped_column(Text)
    material_hash: Mapped[str] = mapped_column(String(64))
    material_length: Mapped[int] = mapped_column(Integer)
    read_ranges: Mapped[list] = mapped_column(JSONB, default=list)
    report: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    round: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("role IN ('reader','reviewer')"),
        CheckConstraint("status IN ('pending','completed','invalid','failed')"),
    )


class LearningAdvance(Base):
    """事务化推进回执；重复完成通知不能安排另一份相同工作。"""

    __tablename__ = "learning_advances"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("learning_tasks.id"), index=True)
    unit_id: Mapped[str] = mapped_column(ForeignKey("learning_units.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), unique=True)
    sequence: Mapped[int] = mapped_column(Integer)
    requirements_revision: Mapped[int] = mapped_column(Integer)
    before_state: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("task_id", "sequence"),)


class LearningProfile(Base):
    """全局或单个任务的人工配置指针；自动部分由有效记忆确定性装配。"""

    __tablename__ = "learning_profiles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    identity_key: Mapped[str] = mapped_column(String(80), unique=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("learning_tasks.id"), unique=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revocation_epoch: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LearningProfileRevision(Base):
    __tablename__ = "learning_profile_revisions"
    profile_id: Mapped[str] = mapped_column(ForeignKey("learning_profiles.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    storage_key: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    source: Mapped[dict] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LearningProfileImport(Base):
    """导入预览是固定文件版本；应用校验哈希和原修订，不能偷偷换内容。"""

    __tablename__ = "learning_profile_imports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    profile_id: Mapped[str] = mapped_column(ForeignKey("learning_profiles.id"), index=True)
    expected_revision: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(200))
    storage_key: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    applied_revision: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ArtifactRetentionReference(Base):
    """真实使用正文的引用；访问目录不登记为引用，不能永久钉住历史材料。"""

    __tablename__ = "artifact_retention_references"
    artifact_id: Mapped[str] = mapped_column(ForeignKey("run_artifacts.id"), primary_key=True)
    owner_kind: Mapped[str] = mapped_column(String(40), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    reason: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_artifact_retention_owner", "owner_kind", "owner_id"),)


class CleanupJob(Base):
    """跨数据库/文件系统的可续跑删除意图；游标只有身份，不复制待删除正文。"""

    __tablename__ = "cleanup_jobs"
    __table_args__ = (
        UniqueConstraint("target_kind", "target_id", name="uq_cleanup_target"),
        CheckConstraint("status IN ('pending','running','failed','completed')"),
        Index("ix_cleanup_due", "status", "next_attempt_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    target_kind: Mapped[str] = mapped_column(String(30))
    target_id: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    phase: Mapped[str] = mapped_column(String(40), default="prepare")
    cursor: Mapped[dict] = mapped_column(JSONB, default=dict)
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[str | None] = mapped_column(String(36))
    claim_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MaintenanceState(Base):
    """周期扫描的租约和键集位置，避免多 API 进程重复从头扫描。"""

    __tablename__ = "maintenance_state"
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    cursor: Mapped[dict] = mapped_column(JSONB, default=dict)
    claim_token: Mapped[str | None] = mapped_column(String(36))
    claim_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_scan_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
