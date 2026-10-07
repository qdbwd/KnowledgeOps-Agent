"""Public state contract for the KnowledgeOps LangGraph."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


TaskKind = Literal["lookup", "research", "audit", "propose_update", "unknown"]
ApprovalState = Literal[
    "not_required",
    "pending",
    "accepted",
    "edited",
    "rejected",
    "timed_out",
]
TerminalReason = Literal[
    "m0_contract_only",
    "completed",
    "needs_user_input",
    "insufficient_evidence",
    "budget_exhausted",
    "failed",
]


class AgentState(TypedDict):
    """Observable state only; hidden model reasoning is never persisted here."""

    request: str
    task_kind: TaskKind
    remaining_steps: int
    approval_state: ApprovalState
    evidence: list[dict[str, object]]
    citations: list[dict[str, object]]
    tool_history: list[dict[str, object]]
    errors: list[dict[str, object]]
    terminal_reason: NotRequired[TerminalReason]
    final_output: NotRequired[str]


def initial_state(request: str, *, max_steps: int = 8) -> AgentState:
    """Create a clean state for one future Trial.

    M0 only validates the state contract. It does not choose tools or call a model.
    """

    normalized = request.strip()
    if not normalized:
        raise ValueError("request must not be blank")
    if max_steps <= 0:
        raise ValueError("max_steps must be greater than zero")
    return {
        "request": normalized,
        "task_kind": "unknown",
        "remaining_steps": max_steps,
        "approval_state": "not_required",
        "evidence": [],
        "citations": [],
        "tool_history": [],
        "errors": [],
    }

