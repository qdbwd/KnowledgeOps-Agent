"""委托整理固定草稿、阅读和审读记录；旧文档不补造学习或审读关系。"""

from copy import deepcopy
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("changesets", sa.Column("learning_unit_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_changeset_learning_unit", "changesets", "learning_units", ["learning_unit_id"], ["id"]
    )
    op.create_index("ix_changesets_learning_unit_id", "changesets", ["learning_unit_id"])
    op.create_table(
        "learning_collaborations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("learning_tasks.id"), nullable=False),
        sa.Column("unit_id", sa.String(36), sa.ForeignKey("learning_units.id"), nullable=False),
        sa.Column("root_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("child_run_id", sa.String(36), sa.ForeignKey("runs.id"), unique=True),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("operation_key", sa.String(200), nullable=False, unique=True),
        sa.Column("requirements_revision", sa.Integer, nullable=False),
        sa.Column("draft_id", sa.String(36), sa.ForeignKey("changesets.id"), nullable=False),
        sa.Column("draft_revision", sa.Integer, nullable=False),
        sa.Column("draft_hash", sa.String(64), nullable=False),
        sa.Column("configuration_hash", sa.String(64), nullable=False),
        sa.Column("input_manifest", JSONB, nullable=False),
        sa.Column("storage_key", sa.Text, nullable=False),
        sa.Column("material_hash", sa.String(64), nullable=False),
        sa.Column("material_length", sa.Integer, nullable=False),
        sa.Column("read_ranges", JSONB, nullable=False),
        sa.Column("report", JSONB, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("round", sa.Integer, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('reader','reviewer')"),
        sa.CheckConstraint("status IN ('pending','completed','invalid','failed')"),
    )
    for key in ("task_id", "unit_id", "root_id"):
        op.create_index(f"ix_learning_collaborations_{key}", "learning_collaborations", [key])
    connection = op.get_bind()
    # 作为可编辑、可禁用的目录角色安装一次。之后普通启动不能重建或重新启用。
    definitions = sa.table(
        "subagent_definitions",
        sa.column("id"),
        sa.column("name"),
        sa.column("display_name"),
        sa.column("description"),
        sa.column("system_prompt"),
        sa.column("tools", JSONB),
        sa.column("skills", JSONB),
        sa.column("tool_groups", JSONB),
        sa.column("resolved_tools", JSONB),
        sa.column("revision"),
        sa.column("authorization_epoch"),
        sa.column("enabled"),
        sa.column("deleted"),
        sa.column("created_at"),
    )
    revisions = sa.table(
        "subagent_definition_revisions",
        sa.column("definition_id"),
        sa.column("revision"),
        sa.column("content", JSONB),
        sa.column("created_at"),
    )
    tools = [
        "read_learning_draft",
        "read_source",
        "inspect_material",
        "search_knowledge",
        "read_knowledge",
        "read_web",
        "search_web",
        "read_tool_result",
    ]
    for role, title, instruction in (
        (
            "reader",
            "学习读者",
            "依据配置找前置知识、例子和衔接的缺口。不要模拟真人授权、满意或已掌握。",
        ),
        (
            "reviewer",
            "独立审读者",
            "对照任务要求、来源与知识规则检查完整草稿，具体指出错误、遗漏、引用或组织问题；不要把读完等同于质量合格。",
        ),
    ):
        identity = "learning-" + role
        row = dict(
            id=identity,
            name=identity,
            display_name=title,
            description=instruction,
            system_prompt=instruction
            + "先使用read_learning_draft补读固定材料，按本次协作合同返回JSON报告。",
            tools=tools,
            skills=["evidence-check", "knowledge-maintenance"],
            tool_groups=None,
            resolved_tools=tools,
            revision=1,
            authorization_epoch=0,
            enabled=True,
            deleted=False,
        )
        if not connection.scalar(sa.select(definitions.c.id).where(definitions.c.name == identity)):
            connection.execute(definitions.insert().values(**row, created_at=datetime.now(UTC)))
            connection.execute(
                revisions.insert().values(
                    definition_id=identity,
                    revision=1,
                    content={**row, "builtin": False},
                    created_at=datetime.now(UTC),
                )
            )
    policies = sa.table(
        "tool_policies", sa.column("id"), sa.column("revision"), sa.column("content", JSONB)
    )
    policy_revisions = sa.table(
        "tool_policy_revisions",
        sa.column("policy_id"),
        sa.column("revision"),
        sa.column("content", JSONB),
        sa.column("created_at"),
    )
    row = connection.execute(sa.select(policies).where(policies.c.id == "default")).mappings().one()
    updated = deepcopy(row["content"])
    for name in ("learning_feedback", "read_learning_draft", "propose_learning_framework"):
        updated["enabled"].setdefault("builtin:" + name, True)
    updated["revision"] += 1
    connection.execute(
        policies.update()
        .where(policies.c.id == "default")
        .values(revision=updated["revision"], content=updated)
    )
    connection.execute(
        policy_revisions.insert().values(
            policy_id="default",
            revision=updated["revision"],
            content=updated,
            created_at=datetime.now(UTC),
        )
    )


def downgrade():
    raise RuntimeError("审读记录不能有损降级，请使用迁移前备份恢复")
