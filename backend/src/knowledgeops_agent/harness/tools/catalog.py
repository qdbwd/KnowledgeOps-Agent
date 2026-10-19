"""工具目录与装配选择：描述能力，不保存函数，也不代替业务授权。

借鉴DeerFlow的name/group及中间件工具收集，保留本项目既有工具和公开SDK。
目录项使用稳定ID；模型名继续沿用原协议，避免旧检查点找不到工具。
来源为middleware的项同样需要收集，不能仅查看create_agent的tools参数。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Literal, cast

from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from knowledgeops_agent.harness.runtime.resources import content_hash

# 仅用于界面；函数名、模型描述及参数Schema仍由真实LangChain工具定义提供。
DISPLAY_NAMES = {
    'start_async_task': '启动子任务', 'check_async_task': '查看子任务',
    'list_async_tasks': '列出子任务', 'update_async_task': '补充子任务要求',
    'cancel_async_task': '取消子任务', 'wait_async_tasks': '等待子任务',
    'search_knowledge': '检索知识', 'list_knowledge': '列出知识文档',
    'knowledge_history': '查看知识历史', 'read_knowledge': '读取知识正文',
    'list_materials': '列出材料', 'list_evidence': '查找已读引用',
    'inspect_material': '查看材料目录', 'read_source': '读取材料正文',
    'read_tool_result': '补读工具结果', 'tool_search': '查找外部工具',
    'update_plan': '更新任务计划', 'ask_clarification': '询问用户',
    'history_search': '检索聊天历史', 'history_read': '读取聊天原话',
    'task_note': '维护任务要点', 'memory_search': '检索长期记忆',
    'memory_read': '读取长期记忆', 'read_learning_draft': '读取学习草稿',
    'wait_learning_input': '等待学习反馈', 'learning_feedback': '请求学习审读',
    'propose_learning_framework': '提出学习框架', 'call_model': '调用辅助模型',
    'prepare_changes': '准备知识草稿', 'publish_changes': '发布知识变更',
    'capture_conversation': '保存讨论材料', 'list_changesets': '列出知识草稿',
    'read_changeset': '读取知识草稿', 'revise_changeset': '修订知识草稿',
    'reject_changeset': '拒绝知识草稿', 'delegate_research': '恢复旧研究任务',
    'search_web': '搜索网页', 'read_web': '读取网页', 'read_file': '读取文件与技能',
    'task': '委派同步子任务', 'ls': '浏览工作区', 'glob': '查找文件',
    'grep': '检索文件内容', 'write_file': '新建文件', 'edit_file': '编辑文件',
    'delete': '删除文件', 'execute': '执行命令', 'import_file': '导入工作区材料',
    'present_files': '交付文件', 'sync_knowledge_files': '同步知识文件',
}


@dataclass(frozen=True)
class ToolDefinition:
    """稳定管理元数据；effect表示业务影响，不表示实现内部完全没有数据库写入。

    subagent_selectable只用于角色白名单。技能文件工具由技能装配决定，委派工具
    由主任务装配决定，它们不会因为存在于总目录就变成子角色的可选权限。
    """

    name: str
    description: str
    group: str
    source: Literal["builtin", "middleware"] = "builtin"
    effect: Literal["read", "write", "run", "model", "delegate"] = "read"
    subagent_selectable: bool = True
    main_only: bool = False
    condition: Literal["always", "auxiliary", "web", "delegation", "skills", "workspace"] = "always"
    legacy_only: bool = False

    def view(self) -> dict:
        """每次返回独立JSON视图，调用方修改它不会改变全局目录。"""
        return {"id": f"{self.source}:{self.name}", **asdict(self),
                "display_name": DISPLAY_NAMES.get(self.name, self.name), "summary": self.description}


# 顺序保留原主/子工具装配顺序，不为了目录展示更改模型看到的默认工具排列。
# 内置角色历史修订仍在subagent_registry中显式保存，不由这里动态扩充许可。
STATIC_TOOLS = (
    *(ToolDefinition(name, description, "delegation", effect="delegate",
                     subagent_selectable=False, main_only=True, condition="delegation")
      for name, description in (
          ("start_async_task", "启动只读子任务，立即返回身份"),
          ("check_async_task", "查看子任务状态和结果"),
          ("list_async_tasks", "列出当前根任务的子任务"),
          ("update_async_task", "向子任务追加有来源的指令"),
          ("cancel_async_task", "取消指定子任务"),
          ("wait_async_tasks", "等待子结果并释放执行槽"),
      )),
    ToolDefinition("search_knowledge", "检索项目知识库", "knowledge_read"),
    ToolDefinition("list_knowledge", "列出知识文档", "knowledge_read"),
    ToolDefinition("knowledge_history", "查看文档历史版本", "knowledge_read"),
    ToolDefinition("read_knowledge", "读取指定知识版本", "knowledge_read"),
    ToolDefinition("list_materials", "列出可用材料", "materials_read"),
    ToolDefinition("list_evidence", "分页查询已读引用及固定版本补读入口", "materials_read"),
    ToolDefinition("inspect_material", "查看材料目录与状态", "materials_read"),
    ToolDefinition("read_source", "读取固定原始材料", "materials_read"),
    ToolDefinition("read_tool_result", "补读已保存的完整工具产物", "run_support"),
    ToolDefinition("tool_search", "查询已授权MCP目录并按需加载参数定义", "run_support",
                   source="middleware", subagent_selectable=False),
    ToolDefinition("update_plan", "记录本次研究计划", "run_support", effect="run"),
    ToolDefinition("ask_clarification", "提出需要真人决定的问题并持久等待回答", "run_support",
                   effect="run", subagent_selectable=False, main_only=True),
    ToolDefinition("history_search", "定位允许继承的会话原始历史", "task_continuity"),
    ToolDefinition("history_read", "分页核对历史原话", "task_continuity"),
    ToolDefinition("task_note", "维护有来源的任务工作要点", "task_continuity", effect="run"),
    ToolDefinition("memory_search", "检索本运行范围内的用户长期记忆", "long_term_memory"),
    ToolDefinition("memory_read", "核对长期记忆的固定修订和原话来源", "long_term_memory"),
    ToolDefinition("read_learning_draft", "读取绑定子Agent的固定草稿与要求", "learning"),
    ToolDefinition(
        "wait_learning_input",
        "保存当前单元待真人决定的问题",
        "learning",
        effect="run",
        subagent_selectable=False,
        main_only=True,
    ),
    ToolDefinition(
        "learning_feedback",
        "对当前单元请求读者或独立审读报告",
        "learning",
        effect="delegate",
        subagent_selectable=False,
        main_only=True,
        condition="delegation",
    ),
    ToolDefinition(
        "propose_learning_framework",
        "保存待确认的有限学习框架",
        "learning",
        effect="run",
        subagent_selectable=False,
        main_only=True,
    ),
    ToolDefinition(
        "call_model",
        "独立处理材料，仍须根任务显式启用辅助模型",
        "auxiliary",
        effect="model",
        condition="auxiliary",
    ),
    ToolDefinition(
        "prepare_changes",
        "准备知识变更草稿",
        "knowledge_maintenance",
        effect="write",
        subagent_selectable=False,
        main_only=True,
    ),
    ToolDefinition(
        "publish_changes",
        "按明确授权发布知识变更",
        "knowledge_maintenance",
        effect="write",
        subagent_selectable=False,
        main_only=True,
    ),
    ToolDefinition(
        "capture_conversation",
        "将讨论保存为项目来源材料",
        "knowledge_maintenance",
        effect="write",
        subagent_selectable=False,
        main_only=True,
    ),
    ToolDefinition(
        "list_changesets",
        "列出已有知识变更草稿",
        "knowledge_maintenance",
        subagent_selectable=False,
        main_only=True,
    ),
    ToolDefinition(
        "read_changeset",
        "读取知识变更及修订",
        "knowledge_maintenance",
        subagent_selectable=False,
        main_only=True,
    ),
    ToolDefinition(
        "revise_changeset",
        "修改知识草稿并保留修订",
        "knowledge_maintenance",
        effect="write",
        subagent_selectable=False,
        main_only=True,
    ),
    ToolDefinition(
        "reject_changeset",
        "拒绝指定修订的知识草稿",
        "knowledge_maintenance",
        effect="write",
        subagent_selectable=False,
        main_only=True,
    ),
    ToolDefinition(
        "delegate_research",
        "恢复历史任务使用的研究委派入口",
        "delegation",
        effect="delegate",
        subagent_selectable=False,
        main_only=True,
        condition="delegation",
        legacy_only=True,
    ),
    ToolDefinition("search_web", "发现公开网页来源，仍受根任务联网限制", "web", condition="web"),
    ToolDefinition("read_web", "读取公开网页，仍受根任务联网限制", "web", condition="web"),
    ToolDefinition(
        "read_file",
        "读取冻结Skill；已绑定工作区的新主任务也能分页读取文件",
        "skills",
        source="middleware",
        subagent_selectable=False,
        condition="skills",
    ),
    ToolDefinition(
        "task",
        "委派已配置的只读子Agent",
        "delegation",
        source="middleware",
        effect="delegate",
        subagent_selectable=False,
        main_only=True,
        condition="delegation",
    ),
    *(
        ToolDefinition(
            name,
            description,
            group,
            source="middleware",
            effect=cast(Literal["read", "write", "run", "model", "delegate"], effect),
            subagent_selectable=False,
            main_only=True,
            condition="workspace",
        )
        for name, description, group, effect in (
            ("ls", "列出工作区文件和目录", "files_read", "read"),
            ("glob", "按路径模式查找工作区文件", "files_read", "read"),
            ("grep", "检索文件内匹配的文本行", "files_read", "read"),
            ("write_file", "在工作区新建文本文件", "files_write", "write"),
            ("edit_file", "精确替换已读文件的文本", "files_write", "write"),
            ("delete", "删除普通工作文件，不删除正式知识", "files_write", "write"),
            ("execute", "在worker运行有时间上限的Linux命令和脚本", "local_execution", "run"),
        )
    ),
    ToolDefinition(
        "import_file",
        "将工作区材料登记为可引用来源",
        "workspace_delivery",
        effect="write",
        subagent_selectable=False,
        main_only=True,
        condition="workspace",
    ),
    ToolDefinition(
        "present_files",
        "核对工作文件并返回用户下载链接",
        "workspace_delivery",
        subagent_selectable=False,
        main_only=True,
        condition="workspace",
    ),
    ToolDefinition(
        "sync_knowledge_files",
        "同步外部编辑或中断后尚未登记的知识工作文件",
        "workspace_delivery",
        effect="write",
        subagent_selectable=False,
        main_only=True,
        condition="workspace",
    ),
)

GROUP_LABELS = {
    "knowledge_read": "知识读取",
    "materials_read": "材料读取",
    "run_support": "运行辅助",
    "task_continuity": "任务连续性",
    "long_term_memory": "长期记忆读取",
    "learning": "学习协作",
    "auxiliary": "辅助模型",
    "knowledge_maintenance": "知识维护",
    "delegation": "子任务委派",
    "web": "网页获取",
    "skills": "技能读取",
    "files_read": "工作区读取",
    "files_write": "工作区编辑",
    "local_execution": "本地命令执行",
    "workspace_delivery": "文件导入与交付",
}


def tool_options(*, child_only: bool = False) -> list[dict]:
    """静态目录供管理入口与装配共同使用；列在目录中不等于当前任务获准使用。"""
    return [item.view() for item in STATIC_TOOLS if not child_only or item.subagent_selectable]


def group_options(*, child_only: bool = False) -> list[dict]:
    """由同一目录生成分组，子角色只展示既有只读白名单中的工具。"""
    tools = tool_options(child_only=child_only)
    groups = [
        {"id": group, "name": label, "tool_ids": members}
        for group, label in GROUP_LABELS.items()
        if (members := [item["id"] for item in tools if item["group"] == group])
    ]
    if child_only:
        groups.append({
            "id": "mcp", "name": "继承只读 MCP", "tool_ids": [],
            "description": "从新根任务已允许的 MCP 目录继承只读工具，不增加服务或写入许可。",
        })
    return groups


def role_tool_names(definition: dict) -> list[str]:
    """新角色使用保存过的组展开结果；旧修订仍使用原有显式tools列表。"""
    resolved = definition.get("resolved_tools")
    return list(definition["tools"] if resolved is None else resolved)


def role_inherits_mcp(definition: dict) -> bool:
    """未指定组时遵循默认继承；显式列表可以收窄，实际成员仍来自根快照。"""
    groups = definition.get("tool_groups")
    return groups is None or "mcp" in groups


def local_tool_reasons(
    item: dict,
    root_input: dict,
    *,
    is_root: bool,
    legacy_snapshot: bool = False,
    role_tools: Sequence[str] | None = None,
) -> list[str]:
    """构图和管理预览共享任务/角色约束；原因码不代替执行时的业务授权。"""
    reasons = []
    if not is_root and item["main_only"]:
        reasons.append("main_only")
    if item["legacy_only"] and not legacy_snapshot:
        reasons.append("legacy_only")
    if item["condition"] == "auxiliary" and root_input.get("auxiliary", False) is not True:
        reasons.append("auxiliary_disabled")
    if item["condition"] == "web" and root_input.get("knowledge_only", False):
        reasons.append("knowledge_only")
    if item["name"] == "execute" and root_input.get("knowledge_only", False):
        reasons.append("knowledge_only")
    if item["condition"] == "delegation" and not root_input.get("delegation", False):
        reasons.append("delegation_disabled")
    if item["source"] == "builtin" and role_tools is not None and item["name"] not in role_tools:
        reasons.append("role_not_selected")
    return reasons


def local_tool_names(
    root_input: dict,
    *,
    is_root: bool,
    legacy_snapshot: bool = False,
    role_tools: Sequence[str] | None = None,
) -> list[str]:
    """合并原有任务选项和角色上限，保持未配置/空许可的区别。

    None表示没有配置角色，继续兼容既有主图或历史子图；空序列表示角色没有
    普通工具。始终先执行根约束，角色白名单不能重新启用联网或辅助模型。
    """
    return [
        item.name
        for item in STATIC_TOOLS
        if item.source == "builtin"
        and not local_tool_reasons(
            item.view(),
            root_input,
            is_root=is_root,
            legacy_snapshot=legacy_snapshot,
            role_tools=role_tools,
        )
    ]


def mcp_tool_id(connection_id: str, original_name: str) -> str:
    """管理身份保留完整连接ID与远端名称，不使用展示名称或被截断的调用名。"""
    return f"mcp:{connection_id}:{original_name}"


def mcp_tool_name(connection_id: str, original_name: str) -> str:
    """沿用已发布的MCP模型调用名称，避免已有消息/检查点发生协议变化。

    这里保留旧算法的短前缀与名称摘要；完整身份另行保存，实际装配时拒绝
    名称碰撞。不能为了目录整洁就悄悄重命名历史工具。
    """
    return (
        "mcp_"
        + connection_id.replace("-", "")[:12]
        + "_"
        + re.sub(r"[^a-zA-Z0-9_-]", "_", original_name)[:28]
        + "_"
        + content_hash({"name": original_name})[:8]
    )


def mcp_tool_options(bindings: Sequence[dict]) -> list[dict]:
    """把既有MCP记录转换为目录视图；不联网、不授权，也不复制连接配置。

    管理入口传当前记录，构图传已冻结快照。目录保留禁用/拒绝项以解释不可用
    原因，但它们不会因此进入模型工具列表。confirm仍须原调用服务逐次核准。
    """
    result = []
    for binding in bindings:
        for item in binding["tools"]:
            reasons = []
            if not binding.get("enabled", True):
                reasons.append("connection_disabled")
            if item["permission"] == "deny":
                reasons.append("permission_denied")
            result.append(
                {
                    "id": mcp_tool_id(binding["id"], item["name"]),
                    "name": mcp_tool_name(binding["id"], item["name"]),
                    "original_name": item["name"],
                    "description": item["description"],
                    "source": "mcp",
                    "group": "mcp",
                    "effect": "read" if item["permission"] == "read" else "external",
                    "subagent_selectable": item["permission"] == "read",
                    "connection_id": binding["id"],
                    "connection_name": binding["name"],
                    "connection_revision": binding["revision"],
                    "connection_enabled": binding.get("enabled", True),
                    "authorization_epoch": binding["authorization_epoch"],
                    "permission": item["permission"],
                    "requires_approval": item["permission"] == "confirm",
                    "unavailable_reasons": reasons,
                    "schema": item["schema"],
                }
            )
    return result


def catalog_view(mcp_bindings: Sequence[dict], *, groups: list[dict] | None = None) -> dict:
    """只读能力目录；具体运行是否可用还要与根选项、角色和业务许可求交。"""
    external = mcp_tool_options(mcp_bindings)
    return {
        "tools": [*tool_options(), *external],
        "groups": [
            *(group_options() if groups is None else groups),
            *(
                [{"id": "mcp", "name": "外部MCP", "tool_ids": [item["id"] for item in external]}]
                if external
                else []
            ),
        ],
        "management": {"mcp": "/api/mcp/connections", "subagents": "/api/subagents"},
    }


def describe_toolset(
    tools: Sequence[BaseTool], middlewares: Sequence, *, mcp_bindings: Sequence[dict] = ()
) -> list[dict]:
    """核对并描述最终装配中的真实工具；不修改SDK对象或工具参数Schema。

    重名会导致LangGraph按名称分派时覆盖能力，因此在构图前明确拒绝。中间件
    的公开tools声明也纳入检查，不依赖LangGraph编译图的私有节点结构。
    """
    static = {item.name: item.view() for item in STATIC_TOOLS}
    external = {item["id"]: item for item in mcp_tool_options(mcp_bindings)}
    # SkillsMiddleware等只处理提示词/状态，本身没有tools声明；文件和委派中间件
    # 才提供工具。使用公开的可选属性，不能假设所有钩子对象都拥有该字段。
    declared = [
        tool for middleware in middlewares for tool in (getattr(middleware, "tools", ()) or ())
    ]
    result = []
    seen = set()
    for tool in [*tools, *declared]:
        if not isinstance(tool, BaseTool):
            raise ValueError("工具目录只接受明确的BaseTool声明")
        if tool.name in seen:
            raise ValueError(f"工具名称冲突：{tool.name}")
        # MCP对象携带完整来源ID，避免短模型名碰撞时把工具错误归到另一连接。
        # 这里只选择已登记元数据，真正的授权仍由原MCP调用服务强制执行。
        identity = (tool.metadata or {}).get("knowledgeops_tool_id")
        definition = external.get(identity) if identity is not None else static.get(tool.name)
        if definition is None or definition["name"] != tool.name:
            raise ValueError(f"工具尚未登记：{tool.name}")
        seen.add(tool.name)
        result.append(
            {
                **definition,
                "schema": convert_to_openai_tool(tool)["function"]["parameters"],
            }
        )
    return result
