"""为新根任务开放补问；历史工具策略与旧资源快照保持不变。"""

from copy import deepcopy
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    policies = sa.table("tool_policies", sa.column("id"), sa.column("revision"), sa.column("content", JSONB))
    revisions = sa.table("tool_policy_revisions", sa.column("policy_id"), sa.column("revision"),
                         sa.column("content", JSONB), sa.column("created_at"))
    row = connection.execute(sa.select(policies).where(policies.c.id == "default")).mappings().one()
    value = deepcopy(row["content"])
    value["enabled"].setdefault("builtin:ask_clarification", True)
    # 显式主工具白名单仍是上限，不将新工具强塞入用户的自定义选择。
    value["revision"] += 1
    connection.execute(policies.update().where(policies.c.id == "default").values(
        revision=value["revision"], content=value,
    ))
    connection.execute(revisions.insert().values(policy_id="default", revision=value["revision"],
                                                content=value, created_at=datetime.now(UTC)))


def downgrade():
    raise RuntimeError("历史许可修订不自动回退；请使用经过核对的备份恢复")
