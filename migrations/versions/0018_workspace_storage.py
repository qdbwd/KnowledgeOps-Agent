"""文件存储引用；文件回填由 migrate_workspace 脚本显式执行并校验。"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "changesets",
        sa.Column(
            "workspace_paths",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("knowledge_documents", sa.Column("workspace_path", sa.Text()))
    op.create_unique_constraint(
        "uq_knowledge_workspace_path", "knowledge_documents", ["workspace_path"]
    )
    op.add_column("knowledge_versions", sa.Column("body_storage_key", sa.Text()))
    op.add_column("knowledge_versions", sa.Column("body_sha256", sa.String(64)))
    op.add_column("run_artifacts", sa.Column("content_storage_key", sa.Text()))


def downgrade():
    # 删除地址前必须显式把正文恢复到 PG，否则旧应用会得到空正文。
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM knowledge_versions WHERE body_storage_key IS NOT NULL) OR EXISTS (SELECT 1 FROM run_artifacts WHERE content_storage_key IS NOT NULL)"
        )
    ):
        raise RuntimeError("restore file-backed content to database before downgrade")
    op.drop_column("run_artifacts", "content_storage_key")
    op.drop_column("knowledge_versions", "body_sha256")
    op.drop_column("knowledge_versions", "body_storage_key")
    op.drop_constraint("uq_knowledge_workspace_path", "knowledge_documents", type_="unique")
    op.drop_column("knowledge_documents", "workspace_path")
    op.drop_column("changesets", "workspace_paths")
