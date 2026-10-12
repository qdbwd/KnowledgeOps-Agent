"""可达历史索引及已授权的任务连续性工具；不重写旧快照、旧工具策略。"""

from copy import deepcopy
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

# 零版本是0015已经发布的恢复合同，此处固定保存，不依赖后续动态工具目录。
BASELINE_IDS = (
    "builtin:search_knowledge",
    "builtin:list_knowledge",
    "builtin:knowledge_history",
    "builtin:read_knowledge",
    "builtin:list_materials",
    "builtin:inspect_material",
    "builtin:read_source",
    "builtin:read_tool_result",
    "builtin:update_plan",
    "builtin:call_model",
    "builtin:prepare_changes",
    "builtin:publish_changes",
    "builtin:capture_conversation",
    "builtin:list_changesets",
    "builtin:read_changeset",
    "builtin:revise_changeset",
    "builtin:reject_changeset",
    "builtin:delegate_research",
    "builtin:search_web",
    "builtin:read_web",
    "middleware:read_file",
    "middleware:task",
)


def upgrade():
    op.create_table(
        "task_history_records",
        sa.Column("scope", sa.String(64), primary_key=True),
        sa.Column("source_id", sa.String(33), primary_key=True),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("search_terms", ARRAY(sa.Text), nullable=False),
    )
    op.create_index(
        "ix_task_history_terms", "task_history_records", ["search_terms"], postgresql_using="gin"
    )
    op.create_table(
        "task_history_batches",
        sa.Column("scope", sa.String(64), primary_key=True),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("source_ids", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    # 本轮用户已批准新增能力；仅给当前配置新增三个尚不存在的开关。
    # 保留现有显式主工具选择/禁用/撤回代次，旧运行冻结的许可不会扩大。
    bind = op.get_bind()
    policies = sa.table(
        "tool_policies", sa.column("id"), sa.column("revision"), sa.column("content", JSONB)
    )
    revisions = sa.table(
        "tool_policy_revisions",
        sa.column("policy_id"),
        sa.column("revision"),
        sa.column("content", JSONB),
        sa.column("created_at"),
    )
    row = bind.execute(sa.select(policies).where(policies.c.id == "default")).mappings().first()
    current = (
        deepcopy(row["content"])
        if row
        else {
            "id": "default",
            "revision": 0,
            "selection_scope": "local_and_middleware",
            "enabled": dict.fromkeys(BASELINE_IDS, True),
            "main_tools": None,
            "main_tool_groups": None,
            "resolved_main_tools": None,
            "tool_epochs": {},
            "main_epochs": {},
        }
    )
    updated = deepcopy(current)
    for name in ("history_search", "history_read", "task_note"):
        updated["enabled"].setdefault("builtin:" + name, True)
    updated["revision"] += 1
    now = datetime.now(UTC)
    if row:
        bind.execute(
            policies.update()
            .where(policies.c.id == "default")
            .values(revision=updated["revision"], content=updated)
        )
    else:
        bind.execute(
            policies.insert().values(id="default", revision=updated["revision"], content=updated)
        )
        bind.execute(
            revisions.insert().values(
                policy_id="default", revision=0, content=current, created_at=now
            )
        )
    bind.execute(
        revisions.insert().values(
            policy_id="default", revision=updated["revision"], content=updated, created_at=now
        )
    )


def downgrade():
    # 不自动丢弃用户新历史和策略修订；回退需先备份并由人工决定如何处理数据。
    raise RuntimeError("0016 contains task history; use a reviewed backup/restore procedure")
