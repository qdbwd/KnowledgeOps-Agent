"""内部文本转固定来源的兼容别名；升级不转换或删除既有正文。"""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("run_artifacts", sa.Column("promoted_version_id", sa.String(36)))
    op.create_foreign_key("fk_run_artifacts_promoted_version", "run_artifacts", "source_versions",
                          ["promoted_version_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_run_artifacts_promoted_version_id", "run_artifacts", ["promoted_version_id"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM run_artifacts WHERE promoted_version_id IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("downgrade refused: fixed material aliases are still in use")
    op.execute("DELETE FROM maintenance_state WHERE name = 'asset-references-v4'")
    op.drop_index("ix_run_artifacts_promoted_version_id", table_name="run_artifacts")
    op.drop_constraint("fk_run_artifacts_promoted_version", "run_artifacts", type_="foreignkey")
    op.drop_column("run_artifacts", "promoted_version_id")
