from pathlib import Path

import pytest

from knowledgeops_agent.legacy.policies import (
    can_write_canonical_knowledge,
    consume_step,
    is_within_directory,
)
from knowledgeops_agent.legacy.state import initial_state


def test_initial_state_rejects_blank_request() -> None:
    with pytest.raises(ValueError, match="must not be blank"):
        initial_state("   ")


def test_only_explicit_acceptance_unlocks_future_write_policy() -> None:
    assert can_write_canonical_knowledge("accepted") is True
    assert can_write_canonical_knowledge("pending") is False
    assert can_write_canonical_knowledge("edited") is False
    assert can_write_canonical_knowledge("rejected") is False


def test_step_budget_cannot_be_overrun() -> None:
    assert consume_step(2) == 1
    with pytest.raises(RuntimeError, match="budget exhausted"):
        consume_step(0)


def test_path_check_uses_resolved_containment(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()

    assert is_within_directory(root / "topic.md", root) is True
    assert is_within_directory(tmp_path / "outside.md", root) is False
