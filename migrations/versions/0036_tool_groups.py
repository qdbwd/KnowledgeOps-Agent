"""把本地静态组迁为可维护目录，旧策略/角色修订和冻结快照保持原文。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

# 固定迁移时的组成员，不从将来变化的运行目录推导历史许可。
SEEDS = (
    (
        "knowledge_read",
        "知识读取",
        "检索、列出和阅读知识文档及历史版本。",
        ["search_knowledge", "list_knowledge", "knowledge_history", "read_knowledge"],
    ),
    (
        "materials_read",
        "材料读取",
        "查找材料、目录与引用，并阅读保存的正文。",
        ["list_materials", "list_evidence", "inspect_material", "read_source"],
    ),
    (
        "run_support",
        "运行辅助",
        "读取工具结果、维护计划、查找外部工具和询问用户。",
        ["read_tool_result", "middleware:tool_search", "update_plan", "ask_clarification"],
    ),
    (
        "task_continuity",
        "任务连续性",
        "查找会话原话并维护当前任务要点。",
        ["history_search", "history_read", "task_note"],
    ),
    (
        "long_term_memory",
        "长期记忆读取",
        "检索和核对当前任务允许使用的长期记忆。",
        ["memory_search", "memory_read"],
    ),
    (
        "learning",
        "学习协作",
        "读取学习草稿、征求反馈并保存待确认的学习安排。",
        [
            "read_learning_draft",
            "wait_learning_input",
            "learning_feedback",
            "propose_learning_framework",
        ],
    ),
    ("auxiliary", "辅助模型", "调用辅助模型，仍须任务显式开启并受共享预算约束。", ["call_model"]),
    (
        "knowledge_maintenance",
        "知识维护",
        "管理知识草稿与发布；发布仍需独立业务授权。",
        [
            "prepare_changes",
            "publish_changes",
            "capture_conversation",
            "list_changesets",
            "read_changeset",
            "revise_changeset",
            "reject_changeset",
        ],
    ),
    (
        "delegation",
        "子任务委派",
        "创建、查看、等待及管理只读子任务。",
        [
            "start_async_task",
            "check_async_task",
            "list_async_tasks",
            "update_async_task",
            "cancel_async_task",
            "wait_async_tasks",
            "delegate_research",
            "middleware:task",
        ],
    ),
    ("web", "网页获取", "搜索和读取公开网页，仍受任务联网选择约束。", ["search_web", "read_web"]),
    ("skills", "技能读取", "按需读取已允许的技能说明和固定资源。", ["middleware:read_file"]),
    (
        "files_read",
        "工作区读取",
        "列出、匹配和检索工作区文件。",
        ["middleware:ls", "middleware:glob", "middleware:grep"],
    ),
    (
        "files_write",
        "工作区编辑",
        "新建、编辑和删除工作区普通文件。",
        ["middleware:write_file", "middleware:edit_file", "middleware:delete"],
    ),
    (
        "local_execution",
        "本地命令执行",
        "在配置的工作区执行有时间限制的命令。",
        ["middleware:execute"],
    ),
    (
        "workspace_delivery",
        "文件导入与交付",
        "导入材料、交付固定文件和同步知识工作文件。",
        ["import_file", "present_files", "sync_knowledge_files"],
    ),
)


def upgrade():
    groups = op.create_table(
        "tool_groups",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("tool_ids", JSONB, nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    versions = op.create_table(
        "tool_group_revisions",
        sa.Column("group_id", sa.String(100), sa.ForeignKey("tool_groups.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("content", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    for identity, name, description, tools in SEEDS:
        data = {
            "id": identity,
            "name": name,
            "description": description,
            "tool_ids": [key if ":" in key else "builtin:" + key for key in tools],
            "revision": 1,
            "deleted": False,
        }
        op.bulk_insert(groups, [data])
        op.bulk_insert(versions, [{"group_id": identity, "revision": 1, "content": data}])


def downgrade():
    connection = op.get_bind()
    # 旧程序无法表达用户改过的组；不能降级后悄悄恢复默认成员或遗失配置。
    if connection.execute(
        sa.text("SELECT count(*) FROM tool_groups WHERE revision != 1 OR deleted")
    ).scalar_one():
        raise RuntimeError("downgrade refused: edited tool groups must be preserved")
    identities = set(connection.execute(sa.text("SELECT id FROM tool_groups")).scalars())
    if identities != {row[0] for row in SEEDS}:
        raise RuntimeError("downgrade refused: custom tool groups must be preserved")
    op.drop_table("tool_group_revisions")
    op.drop_table("tool_groups")
