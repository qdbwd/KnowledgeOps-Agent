"""MCP配置和逐次许可回执；默认空配置，不触发连接或模型调用。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "mcp_connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("authorization_epoch", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("config", JSONB, nullable=False),
        sa.Column("tools", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "mcp_connection_revisions",
        sa.Column(
            "connection_id", sa.String(36), sa.ForeignKey("mcp_connections.id"), primary_key=True
        ),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("content", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "mcp_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column(
            "connection_id", sa.String(36), sa.ForeignKey("mcp_connections.id"), nullable=False
        ),
        sa.Column("connection_revision", sa.Integer(), nullable=False),
        sa.Column("tool_call_id", sa.String(200), nullable=False),
        sa.Column("tool_name", sa.String(200), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("authority_epoch", sa.Integer(), nullable=False),
        sa.Column("decision", JSONB),
        sa.Column("response", JSONB),
        sa.Column("result", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "tool_call_id", name="uq_mcp_run_call"),
    )
    op.create_index("ix_mcp_calls_run_id", "mcp_calls", ["run_id"])


def downgrade():
    for table in ["mcp_calls", "mcp_connection_revisions", "mcp_connections"]:
        op.drop_table(table)
