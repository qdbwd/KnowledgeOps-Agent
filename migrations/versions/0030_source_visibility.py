"""材料可从资料库移除；固定版本及已有引用继续可读。"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sources", sa.Column("deleted_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("sources", "deleted_at")
