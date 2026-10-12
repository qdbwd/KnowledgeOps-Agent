"""M0 LangGraph contract graph.

This graph deliberately terminates after normalization. Real model decisions,
tools, recovery, checkpointing and human approval belong to later milestones.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from knowledgeops_agent.state import AgentState


def normalize_request(state: AgentState) -> dict[str, object]:
    request = state["request"].strip()
    if not request:
        raise ValueError("request must not be blank")
    return {"request": request}


def stop_at_contract_boundary(state: AgentState) -> dict[str, object]:
    return {
        "terminal_reason": "m0_contract_only",
        "final_output": "M0 contract graph completed without model or tool calls.",
    }


def build_contract_graph():
    """Compile the no-model M0 graph used to verify state wiring."""

    builder: StateGraph[AgentState, None, AgentState, AgentState] = StateGraph(AgentState)
    builder.add_node("normalize_request", normalize_request, input_schema=AgentState)
    builder.add_node(
        "stop_at_contract_boundary", stop_at_contract_boundary, input_schema=AgentState
    )
    builder.add_edge(START, "normalize_request")
    builder.add_edge("normalize_request", "stop_at_contract_boundary")
    builder.add_edge("stop_at_contract_boundary", END)
    return builder.compile()
