"""持久暂停/补问/文件回执/异步通知；不伪造历史运行事实。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def _identity(*, root=True):
    columns = [sa.Column("id", sa.String(36), primary_key=True)]
    if root:
        columns.append(sa.Column("root_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False))
    columns.append(sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False))
    return columns


def _times(*, updated=True):
    columns = [sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)]
    if updated:
        columns.append(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    return columns


def upgrade():
    bind = op.get_bind()
    # 0002 的状态检查没有显式命名；查询当前 schema，避免误改其他测试/业务库。
    schema = bind.scalar(sa.text("SELECT current_schema()"))
    for constraint in sa.inspect(bind).get_check_constraints("runs", schema=schema):
        if "status" in constraint["sqltext"]:
            op.drop_constraint(constraint["name"], "runs", type_="check")
    op.create_check_constraint(
        "ck_runs_status", "runs",
        "status IN ('queued','running','interrupted','completed','cancelled','failed',"
        "'budget_exhausted','paused','waiting_input','waiting_children')",
    )
    op.create_table(
        "run_controls",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("pause_requested", sa.Boolean, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("wait_spec", JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "file_operations", *_identity(),
        sa.Column("tool_call_id", sa.String(200), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("workspace", sa.Text, nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("operation", sa.String(30), nullable=False),
        sa.Column("authority_epoch", sa.Integer, nullable=False),
        sa.Column("before_hash", sa.String(64)),
        sa.Column("after_hash", sa.String(64)),
        sa.Column("content_key", sa.Text),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("result", JSONB),
        sa.Column("revision", sa.Integer, nullable=False),
        *_times(), sa.UniqueConstraint("run_id", "tool_call_id"),
    )
    op.create_table(
        "run_questions", *_identity(),
        sa.Column("tool_call_id", sa.String(200), nullable=False),
        sa.Column("round", sa.Integer, nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("authority_epoch", sa.Integer, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("answer", JSONB),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("interrupt_id", sa.String(200)),
        *_times(), sa.UniqueConstraint("run_id", "tool_call_id", "round"),
    )
    op.create_table(
        "mcp_continuations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("mcp_call_id", sa.String(36), sa.ForeignKey("mcp_calls.id"), nullable=False),
        sa.Column("question_id", sa.String(36), sa.ForeignKey("run_questions.id"), nullable=False),
        sa.Column("round", sa.Integer, nullable=False),
        sa.Column("request_state", JSONB, nullable=False),
        sa.Column("input_responses", JSONB),
        sa.Column("status", sa.String(30), nullable=False),
        *_times(), sa.UniqueConstraint("mcp_call_id", "round"),
    )
    op.create_index("ix_mcp_continuations_mcp_call_id", "mcp_continuations", ["mcp_call_id"])
    op.create_table(
        "child_instructions", *_identity(),
        sa.Column("request_key", sa.String(240), nullable=False),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("authority_epoch", sa.Integer, nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        *_times(updated=False), sa.UniqueConstraint("run_id", "request_key"),
    )
    op.create_table(
        "child_notifications", *_identity(),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("event_key", sa.String(80), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        *_times(updated=False), sa.UniqueConstraint("run_id", "generation", "event_key"),
    )
    op.create_table(
        "model_steps", *_identity(root=False),
        sa.Column("step_key", sa.String(200), nullable=False),
        sa.Column("call_id", sa.String(36), nullable=False, unique=True),
        sa.Column("authority_epoch", sa.Integer, nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("request_snapshot", JSONB, nullable=False),
        *_times(updated=False), sa.UniqueConstraint("run_id", "step_key"),
    )
    for name in ("file_operations", "run_questions", "child_instructions", "child_notifications"):
        for column in ("root_id", "run_id"):
            op.create_index(f"ix_{name}_{column}", name, [column])
    op.create_index("ix_model_steps_run_id", "model_steps", ["run_id"])


def downgrade():
    raise RuntimeError("暂停、补问与执行事实不能无损丢弃；请使用备份恢复方案")
