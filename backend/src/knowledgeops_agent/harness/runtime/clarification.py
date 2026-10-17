"""借鉴 DeerFlow 同批补问优先，接入本应用 PG 问题与 LangGraph 公开 interrupt。

after_model 在工具节点前删除其他提议；wrap_tool_call 保护直接从旧工具节点
恢复的检查点。原响应保留在模型回执，删掉的提议不会在回答后自动重做。
"""

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import interrupt

from knowledgeops_agent.app.runs.questions import RunQuestions
from knowledgeops_agent.vendor.deerflow.tool_call_metadata import clone_ai_message_with_tool_calls

QUESTION_TOOLS = {"ask_clarification", "wait_learning_input"}


def await_answer(runtime, request: dict, *, kind: str = "clarification") -> dict:
    context = runtime.context
    if context.run_id != context.root_id or not runtime.tool_call_id:
        raise PermissionError("只有当前主 Agent 可以向真人提出问题")
    service = RunQuestions(context.jobs)
    question = service.ensure(context, runtime.tool_call_id, request, kind=kind)
    # 每次恢复仍调用同一个公开中断位置，由图消费答案，不能按已回答状态跳过。
    interrupt({"run_question_id": question["id"]})
    return service.result(question["id"], context.root_id)


@tool
def ask_clarification(question: str, runtime: ToolRuntime[Any], options: list[str] | None = None,
                      context: str = "") -> dict:
    """遇到会改变方向、范围或正确性的缺失信息时向真人提问并等待回答。

    question 写一个明确的问题，context 简述为什么需要这个决定，options 可提供最多
    八个简短选项；用户始终可以自由回答或拒绝。已经明确的信息不要再问。
    同批提出此工具会阻止其他操作。回答后重新决策，回答本身不代表执行/发布授权。
    """
    return await_answer(runtime, {"question": question, "options": options or [], "context": context})


def question_batch(state):
    last = next((m for m in reversed(state.get("messages", [])) if isinstance(m, AIMessage)), None)
    if last and any(c.get("name") in QUESTION_TOOLS for c in [*last.tool_calls, *last.invalid_tool_calls]):
        return last
    return None


class ClarificationMiddleware(AgentMiddleware):
    def after_model(self, state, runtime):
        last = question_batch(state)
        if last is None:
            return None
        calls = [c for c in last.tool_calls if c["name"] in QUESTION_TOOLS]
        if len(calls) == len(last.tool_calls):
            return None
        runtime.context.jobs.append_event(runtime.context.run_id, "question_siblings_blocked", {
            "tool_call_ids": [c["id"] for c in last.tool_calls if c["name"] not in QUESTION_TOOLS],
        })
        return {"messages": [clone_ai_message_with_tool_calls(last, calls)]}

    def wrap_tool_call(self, request, handler):
        if question_batch(request.state) and request.tool_call["name"] not in QUESTION_TOOLS:
            return ToolMessage(content="未执行：同批含真人补问，收到回答后必须重新决策。",
                               name=request.tool_call["name"], tool_call_id=request.tool_call["id"], status="error")
        return handler(request)
