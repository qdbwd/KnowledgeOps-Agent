"""独立结果版本与已读回执；历史终态初始化已读，未决操作状态保持。"""

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("application_activity_state", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"), sa.CheckConstraint("id = 1"))
    op.execute("INSERT INTO application_activity_state (id, revision) VALUES (1, 1)")
    op.add_column("runs", sa.Column("result_version", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("runs", sa.Column("result_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table("run_read_receipts",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 0"))
    op.execute("""UPDATE runs SET result_version=1, result_at=COALESCE(finished_at, created_at)
        WHERE parent_id IS NULL AND status IN ('completed','cancelled','failed','budget_exhausted','interrupted')""")
    op.execute("""INSERT INTO run_read_receipts (run_id, version, updated_at)
        SELECT id, result_version, CURRENT_TIMESTAMP FROM runs WHERE parent_id IS NULL AND result_version > 0""")


def downgrade():
    raise RuntimeError("downgrade refused: independent read receipts must not be silently discarded")
