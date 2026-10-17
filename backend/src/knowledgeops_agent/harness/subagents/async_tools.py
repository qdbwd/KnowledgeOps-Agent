"""异步委派的模型接口，名称与基础参数参考 Deep Agents 的公开异步接口。

上游接口依赖 Agent Protocol 服务；本项目复用现有 Run/outbox，不另建远程
运行服务。工具、同步 task 兼容入口和 UI 均调用同一持久委派服务。
"""

from langchain.tools import ToolRuntime, tool
from langgraph.types import interrupt
from sqlalchemy import select

from knowledgeops_agent.app.runs.children import ChildRuns
from knowledgeops_agent.harness.runtime.resources import prepare_resources
from knowledgeops_agent.harness.runtime.state import RunContext
from knowledgeops_agent.harness.subagents.registry import SubagentRegistry
from knowledgeops_agent.persistence.models import Run


def start(context, description: str, tool_call_id: str | None, definition=None, *, suffix="0") -> dict:
    if context.run_id != context.root_id or not tool_call_id:
        raise PermissionError("只有主运行的真实工具调用可以委派，不允许递归")
    manifest = prepare_resources(context)
    key = f"{context.run_id}:{tool_call_id}:{suffix}"
    if definition is not None:
        if definition not in manifest.get("subagents", []):
            raise PermissionError("角色不在本任务冻结目录中")
        with context.jobs.sessions() as session:
            existing = session.scalar(select(Run.id).where(
                Run.parent_id == context.root_id, Run.request_key == key,
            ))
        # 撤回阻止新执行，不能阻止领取原失败报告；子 worker 每次构图仍复查
        # 当前许可。start_child 同时校验原调用的任务与角色，不能借此换绑定。
        if existing is None:
            SubagentRegistry(context.jobs.sessions).validate_binding(definition)
    child = context.jobs.start_child(context.root_id, description, operation_key=key, subagent=definition)
    from knowledgeops_agent.learning.collaboration import LearningCollaborationService

    LearningCollaborationService(context.jobs.sessions).bind_child(context, child, key)
    return ChildRuns(context.jobs).get(context.root_id, child["id"])


def wait(context, ids: list[str], mode="all") -> list[dict]:
    if context.run_id != context.root_id or mode not in {"all", "any"} or not ids or len(set(ids)) != len(ids):
        raise ValueError("主运行等待必须指定不重复子任务，以及 all 或 any")
    service = ChildRuns(context.jobs)
    for identifier in ids:
        service.get(context.root_id, identifier)
    # 重入仍到达原 interrupt。真人恢复可能重启同身份的确定失败任务，旧的
    # resume 值不能证明新代次完成；再次等待沿用公开中断，不占住执行槽。
    while True:
        decision = interrupt({"wait_child_ids": ids, "mode": mode})
        rows = [service.get(context.root_id, identifier) for identifier in ids]
        ready = any(not row["unsettled"] for row in rows) if mode == "any" else all(not row["unsettled"] for row in rows)
        if ready or (isinstance(decision, dict) and decision.get("interrupted_by_guidance")):
            return rows


@tool
def start_async_task(description: str, subagent_type: str, runtime: ToolRuntime[RunContext]) -> dict:
    """启动一个已配置的只读子 Agent，立即返回 task_id，主 Agent 可继续独立工作。

    description 写明目标、材料 ID、边界和输出要求；subagent_type 使用可用角色的
    完整名称。最多两个未收束子任务，共享根预算；不继承主聊天、不允许递归或发布。
    无独立工作时用 wait_async_tasks 等待事件，不用模型反复轮询。
    """
    definitions = prepare_resources(runtime.context).get("subagents", [])
    definition = next((row for row in definitions if row["name"] == subagent_type), None)
    if definition is None:
        raise ValueError("未知子角色；可用名称：" + ", ".join(row["name"] for row in definitions))
    return start(runtime.context, description, runtime.tool_call_id, definition)


@tool
def check_async_task(task_id: str, runtime: ToolRuntime[RunContext]) -> dict:
    """读取一个子任务的真实状态、已有结果与错误。task_id 必须来自本根任务的启动回执。"""
    return ChildRuns(runtime.context.jobs).get(runtime.context.root_id, task_id)


@tool
def list_async_tasks(runtime: ToolRuntime[RunContext]) -> list[dict]:
    """列出当前根任务的子 Agent、目标、状态与结果；不访问其他会话的委派。"""
    return ChildRuns(runtime.context.jobs).list(runtime.context.root_id)


@tool
def update_async_task(task_id: str, message: str, runtime: ToolRuntime[RunContext]) -> dict:
    """向未结束的子任务追加要求，安全点应用并撤下旧决策；不会清零预算或扩大工具权限。"""
    if not runtime.tool_call_id:
        raise PermissionError("追加要求必须来自具有持久调用身份的工具请求")
    return ChildRuns(runtime.context.jobs).update(runtime.context.root_id, task_id, message,
                                                 request_key=runtime.tool_call_id, origin="agent")


@tool
def cancel_async_task(task_id: str, runtime: ToolRuntime[RunContext]) -> dict:
    """取消不再需要的子任务，保留已完成结果；不停止兄弟任务，不撤销已经发生的操作。"""
    return ChildRuns(runtime.context.jobs).cancel(runtime.context.root_id, task_id)


@tool
def wait_async_tasks(task_ids: list[str], runtime: ToolRuntime[RunContext], mode: str = "all") -> list[dict]:
    """等待指定子任务完成，all 等待全部，any 等待任一。释放主执行槽，由结果事件接续。

    task_ids 使用原回执中的 ID。等待不新增模型请求，失败也作为真实结果返回。
    """
    return wait(runtime.context, task_ids, mode)


ASYNC_TOOLS = (start_async_task, check_async_task, list_async_tasks,
               update_async_task, cancel_async_task, wait_async_tasks)


def bind_role_catalog(context, tools):
    """异步入口也携带冻结角色目录；复制本次工具说明，不修改进程级共享工具。"""
    if not any(item.name == "start_async_task" for item in tools):
        return tools
    from knowledgeops_agent.harness.subagents.runtime import effective_tools
    from knowledgeops_agent.harness.tools.access import allowed_local_ids

    root_input = context.jobs.get_run(context.root_id)["input"]
    allowed = allowed_local_ids(context, is_root=False)
    descriptions = []
    for definition in (context.resources or {}).get("subagents", []):
        names = effective_tools(definition, root_input, allowed_ids=allowed)
        descriptions.append(f"- {definition['name']}：{definition['description']}；可用工具：{', '.join(names) or '无外部业务工具'}")
    catalog = "\n\n本任务可选角色：\n" + ("\n".join(descriptions) or "没有可用角色，请直接完成任务。")
    return [item.model_copy(update={"description": item.description + catalog})
            if item.name == "start_async_task" else item for item in tools]
