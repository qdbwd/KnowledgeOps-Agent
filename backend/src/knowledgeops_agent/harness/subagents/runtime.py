"""直接复用Deep Agents委派中间件；本模块只桥接本应用的运行身份和角色快照。

上游SubAgentMiddleware负责task结构、角色选择、上下文交付和Command回包。
CompiledSubAgent接入已有持久运行服务，不复制上游循环，也不依赖其私有函数。
"""

from contextvars import ContextVar

from deepagents.middleware.subagents import CompiledSubAgent, SubAgentMiddleware
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda

from knowledgeops_agent.harness.runtime.resources import ResourceUnavailable
from knowledgeops_agent.harness.tools.catalog import local_tool_names, role_tool_names

# 真实工具调用ID只有ToolRuntime提供；上游task不会把它传给Runnable。
# 每个调用线程独立设置/清理，不能用模型生成的任务文本或trace ID充当幂等键。
_dispatch: ContextVar[tuple | None] = ContextVar("knowledgeops_subagent_dispatch", default=None)


class SubagentDispatchMiddleware(AgentMiddleware):
    """在上游task执行期间提供可信运行关联，不改变它的参数Schema和选择逻辑。"""

    def wrap_tool_call(self, request, handler):
        if request.tool_call["name"] != "task":
            return handler(request)
        identifier = request.tool_call.get("id")
        if not identifier:
            raise ValueError("task requires a persistent tool call ID")
        token = _dispatch.set((request.runtime.context, identifier))
        try:
            return handler(request)
        finally:
            _dispatch.reset(token)


def effective_tools(
    definition: dict, root_input: dict, *, allowed_ids: set[str] | None = None
) -> list[str]:
    """角色受根任务选项与全局策略约束，不能借委派重新打开禁用能力。"""
    selected = role_tool_names(definition)
    allowed = set(local_tool_names(root_input, is_root=False, role_tools=selected))
    if allowed_ids is not None:
        allowed &= {name for name in selected if f"builtin:{name}" in allowed_ids}
    # 角色说明和真实构图使用同一可用性规则，但保留用户配置的说明排列顺序。
    return [name for name in selected if name in allowed]


def selected_definition(context, manifest: dict) -> dict | None:
    """只读取子运行持久绑定；旧研究任务没有绑定则保持原兼容行为。"""
    if context.run_id == context.root_id:
        return None
    definition = context.jobs.get_run(context.run_id)["input"].get("subagent")
    if definition is None:
        return None
    if definition not in manifest.get("subagents", []):
        raise ResourceUnavailable("子Agent绑定不在根任务冻结目录中")
    from knowledgeops_agent.harness.subagents.registry import SubagentRegistry

    try:
        SubagentRegistry(context.jobs.sessions).validate_binding(definition)
    except PermissionError as error:
        raise ResourceUnavailable(str(error)) from error
    return definition


def _runnable(parent, definition: dict):
    """只取隔离后的任务消息；不转交父State中的摘要、计划、技能或授权字段。"""

    def invoke(state):
        from knowledgeops_agent.harness.subagents.delegation import delegate_one

        dispatch = _dispatch.get()
        if dispatch is None or dispatch[0] is not parent:
            raise PermissionError("subagent invocation lacks a trusted parent tool call")
        messages = state.get("messages", [])
        if len(messages) != 1 or not isinstance(messages[0], HumanMessage):
            raise ValueError("configured subagents require an isolated task message")
        task = messages[0].content
        if not isinstance(task, str):
            raise ValueError("delegated task must be text")
        # 在当前工具调用中取得可信 ID，重入后仍绑定同一个持久子任务。
        result = delegate_one(parent, definition, task, dispatch[1])
        return {
            "messages": [AIMessage(content=result.get("answer", ""))],
            "structured_response": result,
        }

    return RunnableLambda(invoke)


def build_delegation_middleware(context, backend) -> list:
    """新根任务使用上游task目录；子任务和旧快照不安装，避免递归或虚构历史配置。"""
    root_input = context.jobs.get_run(context.root_id)["input"]
    if context.run_id != context.root_id or not root_input["delegation"]:
        return []
    definitions = (context.resources or {}).get("subagents", [])
    if not definitions:
        return []
    from knowledgeops_agent.harness.tools.access import allowed_local_ids

    child_allowed = allowed_local_ids(context, is_root=False)
    specs = [
        CompiledSubAgent(
            name=definition["name"],
            description=definition["description"]
            + "；可用工具："
            + (
                ", ".join(effective_tools(definition, root_input, allowed_ids=child_allowed))
                or "无外部业务工具"
            ),
            runnable=_runnable(context, definition),
            mode="isolated",
        )
        for definition in definitions
    ]
    return [
        SubagentDispatchMiddleware(),
        SubAgentMiddleware(
            backend=backend,
            subagents=specs,
            system_prompt=(
                "独立且复杂的子问题可用task按需委派，简单任务直接完成。"
                "description写清问题、材料ID和输出要求；子Agent不继承主聊天。"
                "每次最多并行两个，共享本任务预算、不可递归、不可发布知识。"
                "按返回的status/error判断完成，不能把失败或部分报告冒作完成。"
            ),
            task_description=(
                "委派一个独立只读子任务。可在同轮发出最多两个task调用，"
                "简单问题不必委派。可选角色及能力：\n{available_agents}"
            ),
        ),
    ]
