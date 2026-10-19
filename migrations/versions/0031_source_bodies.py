"""正文与原件分开校验；仅扩展结构，不重提取历史正文或删除文件。"""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "source_versions",
        sa.Column(
            "content_hash_kind",
            sa.String(30),
            nullable=False,
            server_default="original",
        ),
    )
    op.add_column("source_versions", sa.Column("body_hash", sa.String(64)))
    op.add_column("source_versions", sa.Column("body_format", sa.String(30)))
    op.add_column("source_versions", sa.Column("original_hash", sa.String(64)))
    op.alter_column("source_versions", "storage_key", existing_type=sa.Text(), nullable=True)


def downgrade():
    # 已产生无原件网页时无法还原旧的必填文件合同；拒绝丢数据式降级。
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM source_versions WHERE storage_key IS NULL "
                "OR content_hash_kind <> 'original')",
            )
        )
        .scalar()
    ):
        raise RuntimeError(
            "body-only source versions require a compatible application; downgrade refused"
        )
    op.alter_column("source_versions", "storage_key", existing_type=sa.Text(), nullable=False)
    for name in ("original_hash", "body_format", "body_hash", "content_hash_kind"):
        op.drop_column("source_versions", name)
