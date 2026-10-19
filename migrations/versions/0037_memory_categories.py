"""增加独立展示类别；保留正文、旧语义、修订及证据，不触发模型或文件操作。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

SEEDS = (
    ("preference", "表达偏好"),
    ("background", "用户背景"),
    ("behavior", "协作习惯"),
    ("cognitive", "思考与学习习惯"),
    ("goal", "持续目标"),
    ("correction", "明确纠正"),
)


def upgrade():
    categories = op.create_table(
        "memory_categories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("name_key", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "uq_memory_category_active_name",
        "memory_categories",
        ["name_key"],
        unique=True,
        postgresql_where=sa.text("NOT deleted"),
    )
    op.add_column("preferences", sa.Column("display_category_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_preference_display_category",
        "preferences",
        "memory_categories",
        ["display_category_id"],
        ["id"],
    )
    op.add_column(
        "preferences",
        sa.Column("classification", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    for key, name in SEEDS:
        identity = "memory-category-" + key
        op.bulk_insert(
            categories,
            [
                {
                    "id": identity,
                    "name": name,
                    "name_key": name,
                    "revision": 1,
                    "deleted": False,
                    "origin": "legacy",
                }
            ],
        )
        op.get_bind().execute(
            sa.text("""UPDATE preferences SET display_category_id=:identity,
          classification='{"status":"classified","source":"legacy"}'::jsonb
          WHERE category=:category"""),
            {"identity": identity, "category": key},
        )


def downgrade():
    # 本轮目录可被用户编辑；降列会不可逆丢失选择，使用升级前完整备份恢复。
    raise RuntimeError("downgrade refused: restore the verified pre-upgrade backup")
