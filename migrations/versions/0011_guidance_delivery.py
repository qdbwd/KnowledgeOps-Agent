"""排队消息转引导及授权分代；不重新投递任何历史消息。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    for name in ["authority_epoch", "applied_authority_epoch"]:
        op.add_column("runs", sa.Column(name, sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "message_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "message_id",
            sa.String(36),
            sa.ForeignKey("chat_messages.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "source_run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False, unique=True
        ),
        sa.Column("target_root_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("target_generation", sa.Integer(), nullable=False),
        sa.Column("authority_epoch", sa.Integer(), nullable=False),
        sa.Column("user_sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_message_deliveries_target_root_id", "message_deliveries", ["target_root_id"]
    )


def downgrade():
    op.drop_table("message_deliveries")
    for name in ["authority_epoch", "applied_authority_epoch"]:
        op.drop_column("runs", name)
