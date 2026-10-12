"""独立保存评估配置、逐案例证据和审读意见。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "evaluation_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("configuration", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "evaluation_results",
        sa.Column(
            "report_id", sa.String(36), sa.ForeignKey("evaluation_reports.id"), primary_key=True
        ),
        sa.Column("key", sa.String(120), primary_key=True),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("review", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("evaluation_results")
    op.drop_table("evaluation_reports")
