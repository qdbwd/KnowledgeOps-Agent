"""Deterministic safety policies shared by graph nodes and tools."""

from __future__ import annotations

from pathlib import Path

from knowledgeops_agent.legacy.state import ApprovalState


def can_write_canonical_knowledge(approval_state: ApprovalState) -> bool:
    """Only an explicit acceptance can unlock a future canonical write tool."""

    return approval_state == "accepted"


def is_within_directory(path: Path, root: Path) -> bool:
    """Return whether *path* resolves inside *root* without relying on strings."""

    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def consume_step(remaining_steps: int) -> int:
    """Consume one Agent decision step and reject execution past the budget."""

    if remaining_steps <= 0:
        raise RuntimeError("agent step budget exhausted")
    return remaining_steps - 1
