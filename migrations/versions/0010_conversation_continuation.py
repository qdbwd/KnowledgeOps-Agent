"""会话单调序号和不可变延续快照。历史只分配排序序号，不补造内部执行消息。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "conversations",
        sa.Column("user_sequence", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "conversations",
        sa.Column("latest_root_sequence", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("conversations", sa.Column("latest_snapshot_id", sa.String(36), nullable=True))
    op.add_column("runs", sa.Column("conversation_sequence", sa.Integer(), nullable=True))
    op.add_column(
        "runs",
        sa.Column("continuation_bound", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("runs", sa.Column("continuation_snapshot_id", sa.String(36), nullable=True))
    op.add_column(
        "chat_messages",
        sa.Column("user_sequence", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute("""WITH ordered AS (
        SELECT id, row_number() OVER (PARTITION BY conversation_id ORDER BY created_at,id) AS seq
        FROM runs WHERE conversation_id IS NOT NULL AND parent_id IS NULL)
        UPDATE runs SET conversation_sequence=ordered.seq FROM ordered WHERE runs.id=ordered.id""")
    op.execute("""UPDATE conversations c SET user_sequence=q.seq FROM
        (SELECT conversation_id, max(conversation_sequence) seq FROM runs GROUP BY conversation_id) q
        WHERE c.id=q.conversation_id""")
    op.execute("""UPDATE chat_messages m SET user_sequence=r.conversation_sequence
        FROM runs r WHERE m.run_id=r.id AND r.conversation_sequence IS NOT NULL""")
    op.create_unique_constraint(
        "uq_conversation_run_sequence", "runs", ["conversation_id", "conversation_sequence"]
    )
    op.create_table(
        "continuation_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False
        ),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column(
            "parent_snapshot_id",
            sa.String(36),
            sa.ForeignKey("continuation_snapshots.id"),
            nullable=True,
        ),
        sa.Column("covered_sequence", sa.Integer(), nullable=False),
        sa.Column("format_version", sa.Integer(), nullable=False),
        sa.Column("content", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "generation", name="uq_continuation_generation"),
    )
    op.create_index(
        "ix_continuation_snapshots_conversation_id", "continuation_snapshots", ["conversation_id"]
    )
    op.create_index("ix_continuation_snapshots_run_id", "continuation_snapshots", ["run_id"])
    op.create_foreign_key(
        "fk_latest_continuation",
        "conversations",
        "continuation_snapshots",
        ["latest_snapshot_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_run_continuation",
        "runs",
        "continuation_snapshots",
        ["continuation_snapshot_id"],
        ["id"],
    )


def downgrade():
    op.drop_constraint("fk_run_continuation", "runs", type_="foreignkey")
    op.drop_constraint("fk_latest_continuation", "conversations", type_="foreignkey")
    op.drop_table("continuation_snapshots")
    op.drop_constraint("uq_conversation_run_sequence", "runs", type_="unique")
    op.drop_column("chat_messages", "user_sequence")
    for name in ["continuation_snapshot_id", "continuation_bound", "conversation_sequence"]:
        op.drop_column("runs", name)
    for name in ["latest_snapshot_id", "latest_root_sequence", "user_sequence"]:
        op.drop_column("conversations", name)
