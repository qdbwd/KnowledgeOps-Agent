"""扩展原偏好记录为通用长期记忆；保留原ID、修订JSON及提取回执。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "preferences",
        sa.Column("category", sa.String(30), nullable=False, server_default="preference"),
    )
    op.add_column(
        "preferences", sa.Column("confidence", sa.Float, nullable=False, server_default="1")
    )
    op.add_column(
        "preferences", sa.Column("revocation_epoch", sa.Integer, nullable=False, server_default="0")
    )
    op.add_column("preferences", sa.Column("confirmed_at", sa.DateTime(timezone=True)))
    op.add_column("preferences", sa.Column("review_after", sa.DateTime(timezone=True)))
    op.create_table(
        "memory_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("preference_id", sa.String(36), sa.ForeignKey("preferences.id"), nullable=False),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("chat_messages.id")),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("quote", sa.Text, nullable=False),
        sa.Column("context_key", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("preference_id", "message_id", name="uq_memory_evidence_message"),
    )
    op.create_index("ix_memory_evidence_preference_id", "memory_evidence", ["preference_id"])
    op.create_table(
        "memory_search_chunks",
        sa.Column("preference_id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("chunk_index", sa.Integer, primary_key=True),
        sa.Column("body_tokens", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(
            ["preference_id", "revision"],
            ["preference_revisions.preference_id", "preference_revisions.revision"],
        ),
    )
    # 只读取本迁移定义的旧列，不能使用未来ORM查询新字段。保留旧修订JSON原样。
    from uuid import uuid4

    from knowledgeops_agent.retrieval.search import split_passages, tokenize

    connection = op.get_bind()
    for row in connection.execute(
        sa.text("""
        SELECT p.id,p.source,p.source_message_id,p.created_at,
               m.role,m.content,m.conversation_id
        FROM preferences p LEFT JOIN chat_messages m ON p.source_message_id=m.id
    """)
    ).mappings():
        quote = row["source"].get("quote", "")
        authentic = row["role"] == "user" and bool(quote) and quote in row["content"]
        connection.execute(
            sa.text("""
            INSERT INTO memory_evidence(id,preference_id,message_id,kind,quote,context_key,created_at)
            VALUES (:id,:preference_id,:message_id,:kind,:quote,:context_key,:created_at)
        """),
            {
                "id": str(uuid4()),
                "preference_id": row["id"],
                "message_id": row["source_message_id"] if authentic else None,
                "kind": row["source"].get("kind", "legacy") if authentic else "legacy",
                "quote": quote if authentic else "",
                "context_key": "conversation:" + row["conversation_id"] if authentic else None,
                "created_at": row["created_at"],
            },
        )
    for row in connection.execute(
        sa.text("SELECT preference_id,revision,content FROM preference_revisions")
    ).mappings():
        for i, passage in enumerate(split_passages(row["content"]["content"])):
            connection.execute(
                sa.text("""
                INSERT INTO memory_search_chunks(preference_id,revision,chunk_index,body_tokens)
                VALUES (:id,:revision,:chunk,:tokens)
            """),
                {
                    "id": row["preference_id"],
                    "revision": row["revision"],
                    "chunk": i,
                    "tokens": tokenize(passage.text),
                },
            )
    op.create_index(
        "ix_memory_chunks_bm25",
        "memory_search_chunks",
        ["preference_id", "revision", "chunk_index", sa.text("(body_tokens::pdb.whitespace)")],
        postgresql_using="paradedb",
    )
    # 本轮已批准的只读记忆工具只进入当前策略的新修订；不改变旧运行和显式选择。
    from copy import deepcopy
    from datetime import UTC, datetime

    policies = sa.table(
        "tool_policies", sa.column("id"), sa.column("revision"), sa.column("content", JSONB)
    )
    revisions = sa.table(
        "tool_policy_revisions",
        sa.column("policy_id"),
        sa.column("revision"),
        sa.column("content", JSONB),
        sa.column("created_at"),
    )
    row = connection.execute(sa.select(policies).where(policies.c.id == "default")).mappings().one()
    updated = deepcopy(row["content"])
    for tool_id in ["builtin:memory_search", "builtin:memory_read"]:
        updated["enabled"].setdefault(tool_id, True)
    updated["revision"] += 1
    connection.execute(
        policies.update()
        .where(policies.c.id == "default")
        .values(
            revision=updated["revision"],
            content=updated,
        )
    )
    connection.execute(
        revisions.insert().values(
            policy_id="default",
            revision=updated["revision"],
            content=updated,
            created_at=datetime.now(UTC),
        )
    )


def downgrade():
    # 即使类别仍全是偏好，来源与策略修订也可能已有新含义。降列会丢证据，
    # 单独回退工具策略又会改写运行依据，因此沿用完整备份恢复路径。
    raise RuntimeError("通用记忆迁移不支持有损降级；请使用迁移前的完整备份恢复")
