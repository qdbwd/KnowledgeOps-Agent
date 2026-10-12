"""长期偏好、不可变修订和解释回执；不扫描或调用模型补造历史偏好。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "preferences",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("scope", sa.String(20), nullable=False),
        sa.Column("topic", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("source_message_id", sa.String(36), sa.ForeignKey("chat_messages.id")),
        sa.Column("source", JSONB, nullable=False),
        sa.Column("supersedes_id", sa.String(36), sa.ForeignKey("preferences.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "preference_revisions",
        sa.Column(
            "preference_id", sa.String(36), sa.ForeignKey("preferences.id"), primary_key=True
        ),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("content", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "preference_extractions",
        sa.Column("message_id", sa.String(36), sa.ForeignKey("chat_messages.id"), primary_key=True),
        sa.Column("extractor_version", sa.String(40), primary_key=True),
        sa.Column("result", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    for table in ["preference_extractions", "preference_revisions", "preferences"]:
        op.drop_table(table)
