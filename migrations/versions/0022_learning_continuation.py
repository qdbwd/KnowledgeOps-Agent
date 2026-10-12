"""自动推进与单元预算；真人序号不再承担执行顺序职责。"""

from copy import deepcopy
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("execution_sequence", "latest_execution_sequence"):
        op.add_column(
            "conversations", sa.Column(name, sa.Integer, nullable=False, server_default="0")
        )
    op.add_column("runs", sa.Column("execution_sequence", sa.Integer))
    op.add_column(
        "runs", sa.Column("learning_accounted_ms", sa.Integer, nullable=False, server_default="0")
    )
    op.add_column(
        "learning_tasks",
        sa.Column("advance_sequence", sa.Integer, nullable=False, server_default="0"),
    )
    # 已执行旧运行的先后关系沿用原合同；不改消息/快照内容及哈希。排队运行领取时分配。
    op.execute(
        "UPDATE runs SET execution_sequence=conversation_sequence WHERE parent_id IS NULL AND status <> 'queued'"
    )
    op.execute(
        "UPDATE conversations c SET execution_sequence=GREATEST(c.user_sequence,c.latest_root_sequence), latest_execution_sequence=c.latest_root_sequence"
    )
    op.create_table(
        "learning_advances",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("learning_tasks.id"), nullable=False),
        sa.Column("unit_id", sa.String(36), sa.ForeignKey("learning_units.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False, unique=True),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("requirements_revision", sa.Integer, nullable=False),
        sa.Column("before_state", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "sequence"),
    )
    for key in ("task_id", "unit_id"):
        op.create_index(f"ix_learning_advances_{key}", "learning_advances", [key])
    connection = op.get_bind()
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
    row = connection.execute(sa.select(policies).where(policies.c.id == "default")).mappings().one()
    value = deepcopy(row["content"])
    value["enabled"].setdefault("builtin:wait_learning_input", True)
    value["revision"] += 1
    connection.execute(
        policies.update()
        .where(policies.c.id == "default")
        .values(revision=value["revision"], content=value)
    )
    connection.execute(
        revisions.insert().values(
            policy_id="default",
            revision=value["revision"],
            content=value,
            created_at=datetime.now(UTC),
        )
    )


def downgrade():
    raise RuntimeError("推进与累计预算记录不能无损降级；请按备份恢复方案操作")
