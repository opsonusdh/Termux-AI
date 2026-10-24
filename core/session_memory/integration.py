"""
Session Memory Integration Hooks

These functions are designed to be called from context_manager.py
at key points in the chunk lifecycle to automatically build the session draft.
"""

from .session_memory import (
    append_session_note,
    get_session_draft,
    finalize_session,
    auto_note_from_chunk,
    get_current_session_id,
)

# Re-export for convenience
__all__ = [
    "append_session_note",
    "get_session_draft",
    "finalize_session",
    "auto_note_from_chunk",
    "get_current_session_id",
    "note_on_chunk_open",
    "note_on_chunk_close",
    "note_on_tool_call",
    "note_on_file_edit",
    "note_on_decision",
    "note_on_error",
    "note_on_finding",
    "note_on_question",
]


# High-level convenience wrappers for common events


def note_on_chunk_open(chunk_id: int, user_input: str) -> None:
    """Call when a new chunk is opened (context_manager.open_chunk)."""
    append_session_note(
        note_type="action",
        content=f"Chunk {chunk_id} opened: {user_input[:150]}",
        chunk_id=chunk_id,
        metadata={"event": "chunk_open", "user_input": user_input[:300]},
    )


def note_on_chunk_close(
    chunk_id: int,
    user_input: str,
    assistant_reply: str,
    tools_used: list[str],
    reasoning_meta: dict | None = None,
) -> None:
    """Call when a chunk is closed (context_manager.close_chunk)."""
    auto_note_from_chunk(chunk_id, user_input, assistant_reply, tools_used)


def note_on_tool_call(chunk_id: int, tool_name: str, args_summary: str, result_summary: str) -> None:
    """Call when a tool is invoked during a chunk."""
    append_session_note(
        note_type="action",
        content=f"Tool: {tool_name} -- {args_summary[:100]}",
        chunk_id=chunk_id,
        metadata={
            "event": "tool_call",
            "tool": tool_name,
            "args_preview": args_summary[:200],
            "result_preview": result_summary[:200],
        },
    )


def note_on_file_edit(chunk_id: int, file_path: str, operation: str, details: str = "") -> None:
    """Call when a file is created, modified, or deleted."""
    append_session_note(
        note_type="file_change",
        content=f"{operation}: {file_path} -- {details[:100]}",
        chunk_id=chunk_id,
        metadata={
            "event": "file_edit",
            "file": file_path,
            "operation": operation,
            "details": details[:300],
        },
    )


def note_on_decision(chunk_id: int, decision: str, reasoning: str = "") -> None:
    """Call when the agent makes a significant decision."""
    append_session_note(
        note_type="decision",
        content=decision,
        chunk_id=chunk_id,
        metadata={
            "event": "decision",
            "reasoning": reasoning[:300],
        },
    )


def note_on_error(chunk_id: int, error: str, context: str = "", recovery: str = "") -> None:
    """Call when an error occurs and how it was handled."""
    content = f"Error: {error[:150]}"
    if recovery:
        content += f" | Recovered: {recovery[:100]}"

    append_session_note(
        note_type="error",
        content=content,
        chunk_id=chunk_id,
        metadata={
            "event": "error",
            "error": error[:300],
            "context": context[:200],
            "recovery": recovery[:200],
        },
    )


def note_on_finding(chunk_id: int, finding: str, source: str = "") -> None:
    """Call when a significant fact is discovered."""
    append_session_note(
        note_type="finding",
        content=finding,
        chunk_id=chunk_id,
        metadata={
            "event": "finding",
            "source": source[:100],
        },
    )


def note_on_question(chunk_id: int, question: str) -> None:
    """Call when an open question is identified."""
    append_session_note(
        note_type="question",
        content=question,
        chunk_id=chunk_id,
        metadata={"event": "question"},
    )


# Example integration snippet for context_manager.py
#
# In context_manager.py, add these imports at the top:
#
#     from workspace.session_memory.integration import (
#         note_on_chunk_open,
#         note_on_chunk_close,
#         note_on_tool_call,
#         note_on_file_edit,
#         note_on_decision,
#         note_on_error,
#     )
#
# Then call at the appropriate places:
#
# In open_chunk():
#     note_on_chunk_open(cid, user_input)
#
# In close_chunk():
#     # Collect tools used from _pending_context
#     tools_used = [m.get("tool_calls", [{}])[0].get("function", {}).get("name", "unknown")
#                   for m in _pending_context if m.get("role") == "assistant" and m.get("tool_calls")]
#     note_on_chunk_close(cid, user_input, assistant_reply, tools_used, reasoning_meta)
#
# In _write_normal_chunk / _write_split_chunk (after successful write):
#     # Could track file modifications if context_manager does file ops
#
# For file edits done via shell/agent, the agent should call note_on_file_edit directly.