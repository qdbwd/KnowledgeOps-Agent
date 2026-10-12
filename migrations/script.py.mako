"""${message}

Revision: ${up_revision}
Previous: ${down_revision | comma,n}
"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade():
    """将数据库升级到本版结构；应用部署前显式执行。"""
    ${upgrades if upgrades else "pass"}


def downgrade():
    """结构回退会删除本版新增数据，只供明确授权的维护场景。"""
    ${downgrades if downgrades else "pass"}
