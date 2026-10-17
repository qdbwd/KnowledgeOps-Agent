"""新任务可查询已授权工具与证据目录；不回填旧快照或显式工具白名单。"""

from copy import deepcopy
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    policies = sa.table("tool_policies", sa.column("id"), sa.column("revision"), sa.column("content", JSONB))
    revisions = sa.table("tool_policy_revisions", sa.column("policy_id"), sa.column("revision"),
                         sa.column("content", JSONB), sa.column("created_at"))
    row = connection.execute(sa.select(policies).where(policies.c.id == "default")).mappings().one()
    value = deepcopy(row["content"])
    for identity in ("middleware:tool_search", "builtin:list_evidence"):
        value["enabled"].setdefault(identity, True)
    value["revision"] += 1
    connection.execute(policies.update().where(policies.c.id == "default").values(revision=value["revision"], content=value))
    connection.execute(revisions.insert().values(policy_id="default", revision=value["revision"], content=value, created_at=datetime.now(UTC)))


def downgrade():
    raise RuntimeError("历史许可修订不自动回退；请使用经过核对的备份恢复")
