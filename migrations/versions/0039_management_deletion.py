"""显式用户删除与自动过期分开；只增加标记，不删除现存内容。"""

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("knowledge_documents", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("user_deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    raise RuntimeError("downgrade refused: removing deletion markers could revive removed records")
