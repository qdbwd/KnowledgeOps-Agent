# Adapted from bytedance/deer-flow commit 127c2c220c30d875b2995e95608b98d86db6bc13.
# MIT license: see LICENSE in this directory. Changes documented in README-continuity.md.
from langchain_core.messages import AIMessage

from .task_continuity_state import (
    MAX_NOTE_CHARS,
    MAX_NOTE_SOURCES,
    MAX_NOTES,
    NOTE_KEY_PATTERN,
    RESOLVED_TOOL_CALL_ARGS_KEY,
    SOURCE_ID_PATTERN,
)


def _note_shape_error(key: str, content: str, sources: list[str]) -> str | None:
    """Share deterministic validation between batch reservation and execution."""
    if (
        not NOTE_KEY_PATTERN.fullmatch(key)
        or len(content) > MAX_NOTE_CHARS
        or len(sources) > MAX_NOTE_SOURCES
    ):
        return "invalid_note"
    if any(
        not isinstance(source_id, str) or not SOURCE_ID_PATTERN.fullmatch(source_id)
        for source_id in sources
    ):
        return "invalid_source_id"
    return None


def _has_note_capacity(runtime, notes: dict, key: str) -> bool:
    """Reserve new keys from the shared batch snapshot so parallel Commands preserve existing notes."""
    if key in notes:
        return True
    available = MAX_NOTES - len(notes)
    if available <= 0:
        return False
    message = next(
        (
            message
            for message in reversed(runtime.state.get("messages", []))
            if isinstance(message, AIMessage)
        ),
        None,
    )
    if message is None or not any(
        call["id"] == runtime.tool_call_id for call in message.tool_calls
    ):
        # Direct tool calls may have no model batch; retain the single-call capacity check.
        return True
    reserved: set[str] = set()
    for call in message.tool_calls:
        if call["name"] != "task_note":
            continue
        # Use middleware-resolved batch arguments when available, matching execution; otherwise use raw arguments.
        args = runtime.state.get(RESOLVED_TOOL_CALL_ARGS_KEY, {}).get(call["id"], call["args"])
        if not isinstance(args, dict):
            continue
        candidate = args.get("key")
        content = args.get("content")
        if not isinstance(candidate, str) or not isinstance(content, str) or not content:
            continue
        sources = args.get("source_ids")
        if sources is None:
            sources = []
        if not isinstance(sources, list) or _note_shape_error(candidate, content, sources):
            continue
        if candidate not in notes and len(reserved) < available:
            reserved.add(candidate)
    # Do not borrow slots from sibling deletions or runtime failures: they have not committed and may be denied.
    return key in reserved
