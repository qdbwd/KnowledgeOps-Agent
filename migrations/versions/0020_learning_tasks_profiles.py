"""持久学习任务与两层配置；旧会话不转任务，旧消息不重新提取。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False
        ),
        sa.Column("request_key", sa.String(200), nullable=False, unique=True),
        sa.Column("creation_input", JSONB, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("requirements_revision", sa.Integer, nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("goal", sa.Text, nullable=False),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("topic", sa.String(200), nullable=False),
        sa.Column("configuration", JSONB, nullable=False),
        sa.Column("mode", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("confirmed_requirements_revision", sa.Integer),
        sa.Column("auto_publish", sa.Boolean, nullable=False),
        sa.Column("authority_epoch", sa.Integer, nullable=False),
        sa.Column("stop_reason", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("mode IN ('interactive','delegated')"),
        sa.CheckConstraint(
            "status IN ('draft','active','paused','waiting','completed','partial','cancelled')"
        ),
    )
    op.create_index("ix_learning_tasks_conversation_id", "learning_tasks", ["conversation_id"])
    op.create_table(
        "learning_task_revisions",
        sa.Column("task_id", sa.String(36), sa.ForeignKey("learning_tasks.id"), primary_key=True),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("content", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "learning_units",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("learning_tasks.id"), nullable=False),
        sa.Column("key", sa.String(80), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("goal", sa.Text, nullable=False),
        sa.Column("dependencies", JSONB, nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("budgets", JSONB, nullable=False),
        *[
            sa.Column(name, sa.Integer, nullable=False)
            for name in (
                "model_calls",
                "tool_calls",
                "searches",
                "active_ms",
                "reader_rounds",
                "review_rounds",
            )
        ],
        sa.Column("draft_id", sa.String(36), sa.ForeignKey("changesets.id")),
        sa.Column("result_version_id", sa.String(36), sa.ForeignKey("knowledge_versions.id")),
        sa.Column("stop_reason", sa.Text),
        sa.UniqueConstraint("task_id", "key"),
        sa.CheckConstraint(
            "model_calls >= 0 AND tool_calls >= 0 AND searches >= 0 AND active_ms >= 0"
        ),
    )
    op.create_index("ix_learning_units_task_id", "learning_units", ["task_id"])
    op.create_table(
        "learning_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("identity_key", sa.String(80), nullable=False, unique=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("learning_tasks.id"), unique=True),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("revocation_epoch", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "learning_profile_revisions",
        sa.Column(
            "profile_id", sa.String(36), sa.ForeignKey("learning_profiles.id"), primary_key=True
        ),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("storage_key", sa.Text, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("source", JSONB, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "learning_profile_imports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "profile_id", sa.String(36), sa.ForeignKey("learning_profiles.id"), nullable=False
        ),
        sa.Column("expected_revision", sa.Integer, nullable=False),
        sa.Column("filename", sa.String(200), nullable=False),
        sa.Column("storage_key", sa.Text, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("applied_revision", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_learning_profile_imports_profile_id", "learning_profile_imports", ["profile_id"]
    )
    op.add_column("conversations", sa.Column("selected_learning_task_id", sa.String(36)))
    op.create_foreign_key(
        "fk_selected_learning_task",
        "conversations",
        "learning_tasks",
        ["selected_learning_task_id"],
        ["id"],
    )
    for column, target in [
        ("learning_task_id", "learning_tasks"),
        ("learning_unit_id", "learning_units"),
    ]:
        op.add_column("runs", sa.Column(column, sa.String(36), sa.ForeignKey(target + ".id")))
        op.create_index("ix_runs_" + column, "runs", [column])
    op.add_column(
        "runs", sa.Column("origin", sa.String(30), nullable=False, server_default="human")
    )
    op.execute("UPDATE runs SET origin='subagent' WHERE parent_id IS NOT NULL")
    op.execute("UPDATE runs SET origin='system' WHERE parent_id IS NULL AND kind <> 'agent'")


def downgrade():
    raise RuntimeError("学习任务与配置已包含独立历史，请使用完整备份恢复，不支持有损降级")
