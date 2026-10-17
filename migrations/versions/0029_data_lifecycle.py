"""增加生命周期与清理意图，不删除正文/详情，旧任务自纳管享有完整宽限。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("deletion_state", sa.String(20), nullable=False, server_default="active"))
    op.add_column("conversations", sa.Column("deletion_requested_at", sa.DateTime(timezone=True)))
    op.add_column("conversations", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.add_column("runs", sa.Column("lifecycle_state", sa.String(20), nullable=False, server_default="active"))
    op.add_column("runs", sa.Column("retention_adopted_at", sa.DateTime(timezone=True)))
    op.add_column("runs", sa.Column("retention_eligible_at", sa.DateTime(timezone=True)))
    op.add_column("runs", sa.Column("details_expires_at", sa.DateTime(timezone=True)))
    op.add_column("runs", sa.Column("details_summary", JSONB, nullable=False, server_default="{}"))
    op.add_column("runs", sa.Column("input_fingerprint", sa.String(64)))
    op.create_index("ix_runs_details_expires_at", "runs", ["details_expires_at"])
    op.add_column("chat_messages", sa.Column("protocol_payload", JSONB))
    op.add_column("child_notifications", sa.Column("consumed_at", sa.DateTime(timezone=True)))
    op.add_column("run_artifacts", sa.Column("expired_at", sa.DateTime(timezone=True)))
    op.alter_column("learning_tasks", "conversation_id", nullable=True)
    # 空的 adopted_at 由分批纳管服务填入真实纳管时刻；迁移不伪造消费或消息协议。
    op.create_table("artifact_retention_references",
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("run_artifacts.id"), primary_key=True),
        sa.Column("owner_kind", sa.String(40), primary_key=True),
        sa.Column("owner_id", sa.String(120), primary_key=True),
        sa.Column("reason", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_artifact_retention_owner", "artifact_retention_references", ["owner_kind", "owner_id"])
    op.create_table("cleanup_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("target_kind", sa.String(30), nullable=False),
        sa.Column("target_id", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("phase", sa.String(40), nullable=False),
        sa.Column("cursor", JSONB, nullable=False),
        sa.Column("summary", JSONB, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("claim_token", sa.String(36)),
        sa.Column("claim_until", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("target_kind", "target_id", name="uq_cleanup_target"),
        sa.CheckConstraint("status IN ('pending','running','failed','completed')"),
    )
    op.create_index("ix_cleanup_due", "cleanup_jobs", ["status", "next_attempt_at"])
    op.create_table("maintenance_state",
        sa.Column("name", sa.String(40), primary_key=True),
        sa.Column("cursor", JSONB, nullable=False),
        sa.Column("claim_token", sa.String(36)),
        sa.Column("claim_until", sa.DateTime(timezone=True)),
        sa.Column("next_scan_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    raise RuntimeError("清理后撤销结构不能还原内容，请使用完整备份集恢复")
