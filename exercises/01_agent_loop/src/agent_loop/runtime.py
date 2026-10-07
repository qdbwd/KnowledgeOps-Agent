"""可逐节点调试的 LangGraph 循环：model → tools → model / END。

采用 LangChain AIMessage/ToolMessage、BaseTool.invoke 和 LangGraph
add_messages；显式展开执行部分，便于观察协议、预算和失败反馈。
"""

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import ToolException
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from agent_loop.models import SYSTEM_PROMPT, DecisionModel
from agent_loop.tools import TOOLS


class AgentState(TypedDict):
    """一次运行的状态；消息可累积，计数和终态以节点返回的新值覆盖。

    add_messages 按消息 ID 合并更新，所以节点只返回新增消息，不能每轮
    再次返回整份历史。answered 仅表示模型给出了文本，不等于答案已验收。
    """

    messages: Annotated[list[AnyMessage], add_messages]
    model_calls: int
    tool_calls: int
    status: Literal["running", "answered", "budget_exhausted", "model_error", "protocol_error"]
    detail: str
    answer: str


def initial_state(question: str) -> AgentState:
    """创建独立运行状态；空任务在调用模型之前失败。"""
    if not question.strip():
        raise ValueError("question must not be blank")
    return {
        "messages": [HumanMessage(content=question.strip())],
        "model_calls": 0,
        "tool_calls": 0,
        "status": "running",
        "detail": "",
        "answer": "",
    }


def build_agent(model: DecisionModel, *, max_model_calls: int = 6, max_tool_calls: int = 8):
    """编译独立 Agent 图；预算分别限制模型请求尝试和工具派发尝试。

    工具结果必须回传后才再次调用模型；可恢复的工具错误也进入消息历史。
    模型异常或消息协议损坏则直接终止。图本身不访问个人库或项目主数据。
    每次 invoke/stream 要传入 initial_state，不能复用上次运行的状态。
    """
    if max_model_calls < 1 or max_tool_calls < 1:
        raise ValueError("budgets must be positive")
    tools_by_name = {tool.name: tool for tool in TOOLS}

    def model_node(state: AgentState) -> dict:
        """构造本轮上下文并取得一次决策；先检查预算，避免超额调用。"""
        if state["model_calls"] >= max_model_calls:
            return {"status": "budget_exhausted", "detail": "model_call_budget"}
        calls = state["model_calls"] + 1
        try:
            response = model.invoke([SystemMessage(content=SYSTEM_PROMPT), *state["messages"]])
        except Exception as exc:
            # 网络异常可能带有请求细节。只记录异常类型，避免将密钥或请求头
            # 拼进控制台/轨迹；需要进一步排查时可在这里设置本地断点。
            return {"model_calls": calls, "status": "model_error", "detail": type(exc).__name__}
        if not isinstance(response, AIMessage):
            return {
                "model_calls": calls,
                "status": "protocol_error",
                "detail": "expected_ai_message",
            }
        update = {"messages": [response], "model_calls": calls}
        if response.invalid_tool_calls:
            return {**update, "status": "protocol_error", "detail": "invalid_tool_call_format"}
        if response.tool_calls:
            ids = [call.get("id") for call in response.tool_calls]
            previous_ids = {
                call["id"]
                for message in state["messages"]
                if isinstance(message, AIMessage)
                for call in message.tool_calls
            }
            if not all(ids) or len(set(ids)) != len(ids) or previous_ids.intersection(ids):
                return {**update, "status": "protocol_error", "detail": "invalid_tool_call_ids"}
            return update
        answer = response.text.strip()
        if not answer:
            return {**update, "status": "protocol_error", "detail": "empty_model_response"}
        return {**update, "status": "answered", "answer": answer}

    def tool_node(state: AgentState) -> dict:
        """执行上一条 AIMessage 的请求，并为每项请求生成匹配的结果。

        本练习顺序执行同一批请求，避免并发隐藏控制流。预算在每次派发前
        检查；中途耗尽时，为剩余请求补上错误结果，保证没有悬空的调用 ID。
        """
        response = state["messages"][-1]
        assert isinstance(response, AIMessage)
        results = []
        attempts = state["tool_calls"]
        exhausted = False
        for call in response.tool_calls:
            error = ""
            if attempts >= max_tool_calls:
                error = "tool_budget_exhausted: 本项请求未执行。"
                exhausted = True
            else:
                attempts += 1
                target = tools_by_name.get(call["name"])
                if target is None:
                    error = "unknown_tool: 请从提供的工具中选择。"
                else:
                    try:
                        # 传完整 ToolCall，而非仅传 args；BaseTool 会把返回值
                        # 包装为带 tool_call_id 的 ToolMessage，保留标准协议。
                        results.append(target.invoke(call))
                        continue
                    except ValidationError:
                        error = "invalid_arguments: 请核对参数 Schema，doc_id 只允许 A 或 B。"
                    except ToolException:
                        error = "tool_error: 工具未完成，请调整动作或说明限制。"
                    except Exception as exc:
                        error = f"tool_error: {type(exc).__name__}"
            results.append(
                ToolMessage(
                    content=error, tool_call_id=call["id"], name=call["name"], status="error"
                )
            )
        return {
            "messages": results,
            "tool_calls": attempts,
            "status": "budget_exhausted" if exhausted else "running",
            "detail": "tool_call_budget" if exhausted else "",
        }

    def after_model(state: AgentState) -> str:
        """终态由 Runtime 设置；只有有效工具请求才进入执行节点。"""
        return "tools" if state["status"] == "running" else END

    def after_tools(state: AgentState) -> str:
        """成功和可恢复的工具错误都回到模型；预算停止则不再请求模型。"""
        return "model" if state["status"] == "running" else END

    graph = StateGraph(AgentState)
    graph.add_node("model", model_node)
    graph.add_node("tools", tool_node)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", after_model, {"tools": "tools", END: END})
    graph.add_conditional_edges("tools", after_tools, {"model": "model", END: END})
    return graph.compile()
