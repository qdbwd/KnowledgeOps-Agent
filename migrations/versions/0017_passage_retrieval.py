"""PG片段索引：从既有正文和可见历史回填，不改写原件、版本、批次或检查点。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    # 镜像提供扩展文件，数据库仍须显式启用；新旧数据卷走同一迁移路径。
    # vector是pg_search 0.26的安装依赖，本项目不生成embedding或使用向量检索。
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_search WITH SCHEMA public CASCADE")
    op.create_table(
        "knowledge_search_chunks",
        sa.Column(
            "document_id",
            sa.String(36),
            sa.ForeignKey("search_entries.document_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("chunk_index", sa.Integer, primary_key=True),
        sa.Column(
            "version_id", sa.String(36), sa.ForeignKey("knowledge_versions.id"), nullable=False
        ),
        sa.Column("start", sa.Integer, nullable=False),
        sa.Column("end", sa.Integer, nullable=False),
        sa.Column("body_tokens", sa.Text, nullable=False),
        sa.Column("title_tokens", sa.Text, nullable=False),
        sa.Column("metadata_tokens", sa.Text, nullable=False),
    )
    op.create_index(
        "ix_knowledge_search_chunks_version_id", "knowledge_search_chunks", ["version_id"]
    )
    op.create_table(
        "task_history_chunks",
        sa.Column("scope", sa.String(64), primary_key=True),
        sa.Column("source_id", sa.String(33), primary_key=True),
        sa.Column("chunk_index", sa.Integer, primary_key=True),
        sa.Column("start", sa.Integer, nullable=False),
        sa.Column("end", sa.Integer, nullable=False),
        sa.Column("body_tokens", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(
            ["scope", "source_id"],
            ["task_history_records.scope", "task_history_records.source_id"],
            ondelete="CASCADE",
        ),
    )
    # 当前小型个人应用一次事务完成回填。失败时Alembic回滚表与数据，不能出现
    # 新版本已生效而一半旧材料搜不到的状态；较大部署应另做分批/双写迁移。
    from knowledgeops_agent.persistence.models import KnowledgeVersion
    from knowledgeops_agent.retrieval.index import add_history_chunks, add_knowledge_chunks

    session = Session(bind=op.get_bind())
    try:
        # 迁移只能查询 0017 当时存在的列，不能随运行时 ORM 增加 0018 文件地址。
        # 仅构造未附加 Session 的兼容值对象，索引函数不负责读取新的存储路径。
        for values in session.execute(
            sa.text(
                "SELECT v.id, v.document_id, v.title, v.body, v.summary, v.category, v.tags "
                "FROM knowledge_versions v JOIN search_entries s ON s.version_id = v.id "
                "WHERE v.archived IS FALSE"
            )
        ).mappings():
            version = KnowledgeVersion(**values)
            add_knowledge_chunks(session, version)
            session.flush()
        for record in session.execute(sa.text("SELECT scope, payload FROM task_history_records")):
            add_history_chunks(session, record.scope, record.payload)
            session.flush()
    finally:
        session.close()
    # 先批量回填再建索引；调用框架DDL接口，BM25和事务索引维护由扩展负责。
    op.create_index(
        "ix_knowledge_chunks_bm25",
        "knowledge_search_chunks",
        [
            "document_id",
            "version_id",
            "chunk_index",
            sa.text("(body_tokens::pdb.whitespace)"),
            sa.text("(title_tokens::pdb.whitespace)"),
            sa.text("(metadata_tokens::pdb.whitespace)"),
        ],
        postgresql_using="paradedb",
    )
    op.create_index(
        "ix_history_chunks_bm25",
        "task_history_chunks",
        ["scope", "source_id", "chunk_index", sa.text("(body_tokens::pdb.whitespace)")],
        postgresql_using="paradedb",
    )


def downgrade():
    # 只删除可重建派生索引；原search_entries与历史原话未变，旧版本仍可运行。
    op.drop_table("task_history_chunks")
    op.drop_table("knowledge_search_chunks")
    # 扩展按数据库共享，可能被其他schema使用；降级不删除扩展或vector。
