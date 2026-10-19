"""记录明确的保存意图；旧来源和草稿一律标记未知，不猜测历史归属或删除文件。"""

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sources", sa.Column("ownership_known", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("changesets", sa.Column("retention_intent", sa.String(20), nullable=False, server_default="legacy"))
    op.create_table(
        "source_uses",
        sa.Column("source_id", sa.String(36), sa.ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("owner_kind", sa.String(20), primary_key=True),
        sa.Column("owner_id", sa.String(120), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("owner_kind IN ('library', 'conversation', 'run')"),
    )
    op.create_index("ix_source_uses_owner", "source_uses", ["owner_kind", "owner_id"])


def downgrade():
    # 丢掉归属再恢复旧程序，会把临时资料重新解释成永久材料；显式拒绝有损降级。
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT EXISTS(SELECT 1 FROM source_uses)")).scalar() or connection.execute(
        sa.text("SELECT EXISTS(SELECT 1 FROM changesets WHERE retention_intent <> 'legacy')")
    ).scalar():
        raise RuntimeError("explicit asset ownership exists; downgrade refused")
    op.drop_table("source_uses")
    op.drop_column("changesets", "retention_intent")
    op.drop_column("sources", "ownership_known")
