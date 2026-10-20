"""本地MCP安装身份；候选与后台租约复用扩展操作表。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "mcp_installations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("connection_id", sa.String(36), nullable=False),
        sa.Column("operation_id", sa.String(36), sa.ForeignKey("extension_operations.id"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("request", postgresql.JSONB(), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in ["connection_id", "operation_id", "status"]:
        op.create_index(f"ix_mcp_installations_{column}", "mcp_installations", [column])


def downgrade():
    raise RuntimeError("downgrade refused: managed MCP files require their ownership records")
