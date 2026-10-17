"""审批与命令回执；旧运行不伪造批准和执行记录。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "execution_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("root_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("tool_call_id", sa.String(200), nullable=False),
        sa.Column("tool_name", sa.String(200), nullable=False),
        sa.Column("arguments", JSONB, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("workspace", sa.Text, nullable=False),
        sa.Column("cwd", sa.Text, nullable=False),
        sa.Column("authority_epoch", sa.Integer, nullable=False),
        sa.Column("batch_key", sa.String(64), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("interrupt_id", sa.String(200)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("decision", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "tool_call_id"),
    )
    op.create_table(
        "command_executions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("root_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("tool_call_id", sa.String(200), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("authority_epoch", sa.Integer, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("result", JSONB),
        sa.Column("history", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "tool_call_id"),
    )
    for name in ("execution_actions", "command_executions"):
        for column in ("root_id", "run_id"):
            op.create_index(f"ix_{name}_{column}", name, [column])
    op.create_table(
        "tool_budget_reservations",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("call_key", sa.String(240), primary_key=True),
        sa.Column("tool_name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    raise RuntimeError("审批和执行事实不能无损丢弃；请使用备份恢复方案")
