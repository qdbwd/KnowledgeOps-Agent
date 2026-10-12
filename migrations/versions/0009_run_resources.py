"""运行资源快照与撤回；旧运行标旧格式，不伪造它们当时使用的资源。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "runs", sa.Column("format_version", sa.Integer(), nullable=False, server_default="1")
    )
    op.create_table(
        "run_resource_snapshots",
        sa.Column("root_id", sa.String(36), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("content", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "resource_revocations",
        sa.Column("kind", sa.String(30), primary_key=True),
        sa.Column("resource", sa.Text(), primary_key=True),
        sa.Column("content_hash", sa.String(64), primary_key=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("resource_revocations")
    op.drop_table("run_resource_snapshots")
    op.drop_column("runs", "format_version")
