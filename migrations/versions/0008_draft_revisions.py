"""记录草稿固定修订及编辑许可来源；旧内容只保存当前可见修订，不补造历史。"""

import hashlib
import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("changesets", sa.Column("publication_root_id", sa.String(120), nullable=True))
    op.add_column(
        "changesets",
        sa.Column("validated_evidence_ids", JSONB, nullable=False, server_default="[]"),
    )
    op.execute("UPDATE changesets SET publication_root_id=root_id")
    op.create_table(
        "changeset_revisions",
        sa.Column("changeset_id", sa.String(36), sa.ForeignKey("changesets.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("operation_format", sa.Integer(), nullable=False),
        sa.Column("operations", JSONB, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("actor_run_id", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "operation_receipts",
        sa.Column("operation_key", sa.String(200), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("result", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    connection = op.get_bind()
    for row in connection.execute(
        sa.text("SELECT id,revision,operations,operation_format,created_at FROM changesets")
    ).mappings():
        content = json.dumps(row["operations"], ensure_ascii=False, sort_keys=True)
        connection.execute(
            sa.text("""INSERT INTO changeset_revisions
            (changeset_id,revision,operation_format,operations,content_hash,actor_run_id,created_at)
            VALUES (:id,:revision,:operation_format,CAST(:operations AS jsonb),:hash,NULL,:created_at)"""),
            {**row, "operations": content, "hash": hashlib.sha256(content.encode()).hexdigest()},
        )


def downgrade():
    op.drop_table("operation_receipts")
    op.drop_table("changeset_revisions")
    op.drop_column("changesets", "validated_evidence_ids")
    op.drop_column("changesets", "publication_root_id")
