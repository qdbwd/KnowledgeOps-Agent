"""会话浏览修订、活动排序和提交身份；既有正文、任务和协议不改写。"""

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("creation_key", sa.String(200), nullable=True))
    op.add_column("conversations", sa.Column("creation_fingerprint", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_conversation_creation_key", "conversations", ["creation_key"])
    op.add_column("conversations", sa.Column("activity_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("conversations", sa.Column("messages_revision", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("runs", sa.Column("submission_fingerprint", sa.String(64), nullable=True))
    op.create_table("conversation_index_state", sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"), sa.CheckConstraint("id = 1"))
    op.execute("INSERT INTO conversation_index_state (id, revision) VALUES (1, 1)")
    op.execute("""UPDATE conversations AS c SET activity_at = m.last_at, messages_revision = 1
        FROM (SELECT conversation_id, max(created_at) AS last_at FROM chat_messages GROUP BY conversation_id) AS m
        WHERE c.id = m.conversation_id""")
    op.create_index("ix_conversations_activity_at", "conversations", ["activity_at"])
    # 显式要求当前PG镜像已有扩展；失败回滚整个迁移，不静默更换存储或镜像。
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
    connection = op.get_bind()
    schema = connection.execute(sa.text("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='pg_trgm'")).scalar_one()
    operator = connection.dialect.identifier_preparer.quote(schema) + ".gin_trgm_ops"
    op.execute(f"CREATE INDEX ix_conversations_title_trgm ON conversations USING gin (title {operator})")
    op.execute(f"CREATE INDEX ix_chat_messages_content_trgm ON chat_messages USING gin (content {operator})")


def downgrade():
    raise RuntimeError("downgrade refused: browser submission receipts must not be silently discarded")
