from knowledgeops_agent.legacy.graph import build_contract_graph
from knowledgeops_agent.legacy.state import initial_state


def test_m0_graph_normalizes_and_stops_without_agent_claims() -> None:
    result = build_contract_graph().invoke(initial_state("  检查知识库关系  "))

    assert result["request"] == "检查知识库关系"
    assert result["terminal_reason"] == "m0_contract_only"
    assert result["tool_history"] == []
    assert result["evidence"] == []
