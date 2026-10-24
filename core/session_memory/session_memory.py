"""
Session Memory System for Termux-AI

Provides a session resume artifact that captures what happened in a session,
compressed but detailed enough to continue from where we left off.

Architecture:
- workspace/session_draft.json  --  Incremental draft updated during the session
- logs/session_<timestamp>.json  --  Finalized, compressed session record
- Uses delegate_subtask with small model (north-mini-code:free) for compression
"""

import sys
import json
import os
import glob
import threading
from datetime import datetime
from typing import Any, Optional

# Path bootstrap - core/ must be first so 'import tools' resolves correctly
_CORE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_CORE))  # Termux-AI root
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)
if _ROOT not in sys.path:
    sys.path.insert(1, _ROOT)

import paths


# Configuration
DRAFT_FILE = os.path.join(paths.WORKSPACE_DIR, "session_draft.json")
SESSIONS_DIR = paths.LOGS_DIR  # logs/session_<timestamp>.json

# Compression model (free on OpenRouter, 256k context)
COMPRESSION_MODEL = "cohere/north-mini-code:free"
COMPRESSION_MAX_TOKENS = 4096

# Note types for structured draft
NOTE_TYPES = {
    "decision": "A choice made with reasoning",
    "action": "Something the agent did (tool call, file edit, command)",
    "finding": "Discovered fact from file, command output, or search",
    "file_change": "File created, modified, or deleted",
    "question": "Open question that needs resolution",
    "summary": "High-level summary of a phase",
    "error": "Error encountered and how it was handled",
}


# In-Memory State
_draft_lock = threading.Lock()
_current_session_id: Optional[str] = None


# Core Functions

def _ensure_dirs() -> None:
    """Ensure workspace and logs directories exist."""
    os.makedirs(paths.WORKSPACE_DIR, exist_ok=True)
    os.makedirs(paths.LOGS_DIR, exist_ok=True)


def _load_draft() -> dict:
    """Load the current session draft from disk."""
    if not os.path.exists(DRAFT_FILE):
        return _create_empty_draft()
    try:
        with open(DRAFT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _create_empty_draft()


def _create_empty_draft() -> dict:
    """Create a fresh empty draft structure."""
    global _current_session_id
    session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    _current_session_id = session_id
    return {
        "session_id": session_id,
        "started": datetime.now().isoformat(),
        "updated": datetime.now().isoformat(),
        "chunk_range": [None, None],  # [first_chunk_id, last_chunk_id]
        "steps": [],
        "key_outcomes": [],
        "decisions": [],
        "open_questions": [],
        "files_modified": [],
        "tools_used": [],
        "token_estimate": 0,
    }


def _save_draft(draft: dict) -> None:
    """Save the draft to disk atomically."""
    _ensure_dirs()
    tmp = DRAFT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DRAFT_FILE)


# Public API

def append_session_note(
    note_type: str,
    content: str,
    chunk_id: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """
    Append a note to the current session draft.

    Call this after every significant agent step:
    - decision: "Chose approach X because Y"
    - action: "Edited core/context_manager.py to add session memory hooks"
    - finding: "Found that chunk_summaries.json already has 3-level compression"
    - file_change: "Created workspace/session_memory/session_memory.py"
    - question: "Should we auto-save every N turns or only on command?"
    - error: "Permission denied on config/api.keys, user approved after prompt"

    Args:
        note_type: One of NOTE_TYPES keys
        content: Human-readable description
        chunk_id: Optional parent chunk ID this relates to
        metadata: Optional extra data (file paths, tool names, etc.)

    Returns:
        The updated draft dict
    """
    if note_type not in NOTE_TYPES:
        note_type = "action"  # default fallback

    with _draft_lock:
        draft = _load_draft()
        step = {
            "ts": datetime.now().isoformat(),
            "type": note_type,
            "content": content,
        }
        if chunk_id is not None:
            step["chunk_id"] = chunk_id
        if metadata:
            step["metadata"] = metadata

        draft["steps"].append(step)
        draft["updated"] = datetime.now().isoformat()

        # Track chunk range
        if chunk_id is not None:
            if draft["chunk_range"][0] is None:
                draft["chunk_range"][0] = chunk_id
            draft["chunk_range"][1] = chunk_id

        # Track tools used
        if metadata and "tool" in metadata:
            tool = metadata["tool"]
            if tool not in draft["tools_used"]:
                draft["tools_used"].append(tool)

        # Track files modified
        if metadata and "file" in metadata:
            f = metadata["file"]
            if f not in draft["files_modified"]:
                draft["files_modified"].append(f)

        # Track decisions and questions separately for easy extraction
        if note_type == "decision":
            draft["decisions"].append(content)
        elif note_type == "question":
            draft["open_questions"].append(content)

        _save_draft(draft)
        return draft


def get_session_draft() -> dict:
    """Get the current session draft (for inspection)."""
    with _draft_lock:
        return _load_draft()


def finalize_session(
    use_small_model: bool = True,
    model: str = COMPRESSION_MODEL,
    max_tokens: int = COMPRESSION_MAX_TOKENS,
) -> str:
    """
    Finalize the current session: compress draft into a session file.

    Args:
        use_small_model: If True, use delegate_subtask with small model for compression.
                        If False, just structure the draft as-is (faster, less compressed).
        model: Model to use for compression (only if use_small_model=True)
        max_tokens: Max tokens for compression model

    Returns:
        Path to the created session file
    """
    with _draft_lock:
        draft = _load_draft()

    if not draft["steps"]:
        return "[ERROR] No session draft to finalize."

    # Mark end time
    draft["ended"] = datetime.now().isoformat()

    # Build summary from draft
    summary_parts = []
    if draft["key_outcomes"]:
        summary_parts.append("Key outcomes: " + "; ".join(draft["key_outcomes"][:5]))
    if draft["decisions"]:
        summary_parts.append("Decisions: " + "; ".join(draft["decisions"][:5]))
    draft["summary"] = " | ".join(summary_parts) if summary_parts else "Session completed."

    # Estimate tokens (rough: 1 token ≈ 4 chars)
    draft["token_estimate"] = len(json.dumps(draft)) // 4

    # Prepare session file
    session_id = draft["session_id"]
    session_file = os.path.join(SESSIONS_DIR, f"session_{session_id}.json")

    if use_small_model:
        # Delegate to small model for intelligent compression
        compressed = _compress_with_small_model(draft, model, max_tokens)
        if compressed:
            final_data = compressed
        else:
            # Fallback: use draft as-is
            final_data = draft
    else:
        final_data = draft

    # Write final session file
    _ensure_dirs()
    with open(session_file, "w", encoding="utf-8") as f:
        json.dump(final_data, f, ensure_ascii=False, indent=2)

    # Clear the draft
    if os.path.exists(DRAFT_FILE):
        os.unlink(DRAFT_FILE)

    global _current_session_id
    _current_session_id = None

    return str(session_file)


def _compress_with_small_model(draft: dict, model: str, max_tokens: int) -> Optional[dict]:
    """
    Use delegate_subtask to compress the draft with a small model.
    Returns the compressed session dict or None on failure.
    """
    try:
        from tools import delegate_subtask
    except ImportError:
        return None

    # Build the compression prompt
    prompt = f"""Compress this session draft into a concise but complete session record.

## Draft Data
{json.dumps(draft, indent=2)}

## Requirements
1. Preserve ALL of these fields: session_id, started, ended, chunk_range, summary, key_outcomes, decisions, open_questions, files_modified, tools_used, token_estimate
2. For 'steps': keep only the most significant ones (decisions, key actions, errors, findings). Drop routine tool calls. Max 15 steps.
3. Each kept step should have: ts, type, content (compressed), chunk_id if present.
4. Output MUST be valid JSON only -- no markdown, no explanation.
5. Target under {max_tokens} tokens total.
6. Be dense and factual. No fluff.

Return the compressed session object as JSON."""

    result = delegate_subtask(
        task=prompt,
        context="",
        model=model,
        max_tokens=max_tokens,
        available_tools=[],  # No tools needed for compression
        tool_call_iterations=0,
        system_prompt=(
            "You are a compression engine. Output only valid JSON. "
            "No markdown, no commentary, no reasoning traces."
        ),
    )

    # Parse the result
    try:
        # Extract JSON from the response (may have header text)
        content = result.strip()
        # Find the first { and last }
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            json_str = content[start:end+1]
            return json.loads(json_str)
    except Exception:
        pass

    return None


def list_sessions() -> list[dict]:
    """
    List all saved session files with their metadata.

    Returns:
        List of dicts with: session_id, file_path, started, ended, summary, step_count
    """
    _ensure_dirs()
    sessions = []

    pattern = os.path.join(SESSIONS_DIR, "session_*.json")
    for f in sorted(glob.glob(pattern)):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            fname = os.path.basename(f)
            sessions.append({
                "session_id": data.get("session_id", fname.replace("session_", "").replace(".json", "")),
                "file_path": f,
                "started": data.get("started", ""),
                "ended": data.get("ended", ""),
                "summary": data.get("summary", ""),
                "step_count": len(data.get("steps", [])),
                "token_estimate": data.get("token_estimate", 0),
            })
        except Exception:
            continue

    return sessions


def resume_session(session_id: str) -> dict:
    """
    Load a session file and return its data for context injection.

    Args:
        session_id: The session ID (e.g., "2026-08-31_14-32-07") or full filename.

    Returns:
        The session data dict, or error dict with 'error' key.
    """
    # Handle both "2026-08-31_14-32-07" and "session_2026-08-31_14-32-07.json"
    if not session_id.startswith("session_"):
        session_id = f"session_{session_id}"
    if not session_id.endswith(".json"):
        session_id += ".json"

    session_file = os.path.join(SESSIONS_DIR, session_id)

    if not os.path.exists(session_file):
        # Try to find by partial match
        search_id = session_id.replace("session_", "").replace(".json", "")
        pattern = os.path.join(SESSIONS_DIR, f"session_*{search_id}*.json")
        matches = glob.glob(pattern)
        if len(matches) == 1:
            session_file = matches[0]
        elif len(matches) > 1:
            return {"error": f"Ambiguous session ID: {len(matches)} matches"}
        else:
            return {"error": f"Session not found: {session_id}"}

    try:
        with open(session_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"error": f"Failed to load session: {e}"}


def delete_session(session_id: str) -> str:
    """
    Delete a session file.

    Args:
        session_id: The session ID or filename.

    Returns:
        Success or error message.
    """
    if not session_id.startswith("session_"):
        session_id = f"session_{session_id}"
    if not session_id.endswith(".json"):
        session_id += ".json"

    session_file = os.path.join(SESSIONS_DIR, session_id)

    if not os.path.exists(session_file):
        return f"[ERROR] Session not found: {session_id}"

    try:
        os.unlink(session_file)
        return f"Deleted session: {session_id}"
    except Exception as e:
        return f"[ERROR] Failed to delete: {e}"


def get_current_session_id() -> Optional[str]:
    """Get the current active session ID, if any."""
    global _current_session_id
    if _current_session_id:
        return _current_session_id
    draft = _load_draft()
    return draft.get("session_id")


# Convenience: Auto-append on chunk close (for integration)

def auto_note_from_chunk(chunk_id: int, user_msg: str, assistant_reply: str, tools_used: list) -> None:
    """
    Convenience: generate a draft note from a completed chunk.
    Call from context_manager.close_chunk() or similar.
    """
    # Determine note type based on content
    if "error" in assistant_reply.lower() or "failed" in assistant_reply.lower():
        note_type = "error"
    elif tools_used:
        note_type = "action"
    else:
        note_type = "summary"

    content = f"Chunk {chunk_id}: {user_msg[:100]}"
    if len(user_msg) > 100:
        content += "..."

    append_session_note(
        note_type=note_type,
        content=content,
        chunk_id=chunk_id,
        metadata={
            "tools": tools_used,
            "user_preview": user_msg[:200],
            "assistant_preview": assistant_reply[:200],
        },
    )


# For direct script execution
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m workspace.session_memory <command> [args]")
        print("Commands:")
        print("  list                    - List all sessions")
        print("  resume <session_id>     - Load and show session")
        print("  delete <session_id>     - Delete a session")
        print("  draft                   - Show current draft")
        print("  finalize                - Finalize current draft")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "list":
        sessions = list_sessions()
        if not sessions:
            print("No saved sessions.")
        else:
            print(f"Found {len(sessions)} session(s):\n")
            for s in sessions:
                print(f"  {s['session_id']}")
                print(f"    Started:  {s['started']}")
                print(f"    Ended:    {s['ended'] or '(in progress)'}")
                print(f"    Steps:    {s['step_count']}")
                print(f"    Tokens:   ~{s['token_estimate']}")
                print(f"    Summary:  {s['summary'][:120]}")
                print()
    elif cmd == "resume" and len(sys.argv) > 2:
        result = resume_session(sys.argv[2])
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif cmd == "delete" and len(sys.argv) > 2:
        print(delete_session(sys.argv[2]))
    elif cmd == "draft":
        print(json.dumps(get_session_draft(), indent=2, ensure_ascii=False))
    elif cmd == "finalize":
        print(finalize_session())
    else:
        print(f"Unknown command: {cmd}")