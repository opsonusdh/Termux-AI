"""
Session Memory System for Termux-AI

Provides a session resume artifact that captures what happened in a session,
compressed but detailed enough to continue from where we left off.

Architecture:
- workspace/session_draft.json  --  Incremental draft updated during the session
- logs/session_<timestamp>.json  --  Finalized, compressed session record
- Uses delegate_subtask with small model (north-mini-code:free) for compression
"""

from .session_memory import (
    append_session_note,
    finalize_session,
    list_sessions,
    resume_session,
    delete_session,
    get_session_draft,
    get_current_session_id,
    auto_note_from_chunk,
)

__all__ = [
    "append_session_note",
    "finalize_session",
    "list_sessions",
    "resume_session",
    "delete_session",
    "get_session_draft",
    "get_current_session_id",
    "auto_note_from_chunk",
]