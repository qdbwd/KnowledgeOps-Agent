"""会话运行设置与命令规则；只新增，不改写历史运行或用户消息。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("runtime_settings", JSONB, nullable=False, server_default="{}"))
    op.add_column("conversations", sa.Column("runtime_revision", sa.Integer, nullable=False, server_default="0"))
    op.create_table(
        "execution_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace", sa.Text, nullable=False),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id")),
        sa.Column("cwd", sa.Text, nullable=False),
        sa.Column("argv", JSONB, nullable=False),
        sa.Column("match", sa.String(20), nullable=False),
        sa.Column("effect", sa.String(20), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("deleted", sa.Boolean, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("source_action_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("effect IN ('allow','ask','deny')"),
        sa.CheckConstraint("match IN ('exact','prefix')"),
    )
    op.create_index("ix_execution_rules_conversation_id", "execution_rules", ["conversation_id"])


def downgrade():
    raise RuntimeError("权限规则和执行事实不静默删除；请使用备份恢复方案")
