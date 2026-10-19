"""实际正文引用图；结构升级不回填正文、不触发删除，回填由显式维护分批完成。"""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "asset_references",
        sa.Column("owner_kind", sa.String(30), primary_key=True),
        sa.Column("owner_id", sa.String(200), primary_key=True),
        sa.Column("target_node", sa.String(200), primary_key=True),
        sa.Column("origin_node", sa.String(200), nullable=False),
    )
    op.create_index("ix_asset_references_origin", "asset_references", ["origin_node"])
    op.create_index("ix_asset_references_target", "asset_references", ["target_node"])


def downgrade():
    op.execute("DELETE FROM maintenance_state WHERE name = 'asset-references-v3'")
    op.drop_table("asset_references")  # 派生边可从仍保留的正文重新建立。
