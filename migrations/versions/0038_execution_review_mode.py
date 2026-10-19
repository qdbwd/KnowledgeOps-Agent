"""旧自动执行降为请求批准；不改历史快照、不发模型请求、不删除业务数据。"""

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""UPDATE conversations
        SET runtime_settings = jsonb_set(runtime_settings, '{permission_mode}', '"ask"'::jsonb),
            runtime_revision = runtime_revision + 1
        WHERE runtime_settings->>'permission_mode' = 'auto'""")


def downgrade():
    raise RuntimeError("downgrade refused: cannot restore automatic execution permission")
