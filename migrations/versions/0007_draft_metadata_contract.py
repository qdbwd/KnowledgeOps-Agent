"""为旧草稿保留语义版本；不改写其正文、审批记录或发布回执。"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    # 既有行全部为1；新应用在创建/修订时显式写入2，旧代码写入仍标记为旧格式。
    op.add_column(
        "changesets",
        sa.Column("operation_format", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade():
    op.drop_column("changesets", "operation_format")
