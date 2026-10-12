"""统一工具策略及角色组选择；旧行不重写，旧修订与许可保持原文。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("subagent_definitions", sa.Column("tool_groups", JSONB, nullable=True))
    op.add_column("subagent_definitions", sa.Column("resolved_tools", JSONB, nullable=True))
    op.create_table(
        "tool_policies",
        sa.Column("id", sa.String(30), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content", JSONB, nullable=False),
    )
    op.create_table(
        "tool_policy_revisions",
        sa.Column("policy_id", sa.String(30), sa.ForeignKey("tool_policies.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("content", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("tool_policy_revisions")
    op.drop_table("tool_policies")
    op.drop_column("subagent_definitions", "resolved_tools")
    op.drop_column("subagent_definitions", "tool_groups")
