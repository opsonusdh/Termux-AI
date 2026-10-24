# Session Memory System

A session resume artifact system for Termux-AI that captures what happened in a session,
compressed but detailed enough to continue from where you left off.

## Architecture

```
workspace/session_draft.json     -- Incremental draft (updated during session)
logs/session_<timestamp>.json    -- Finalized, compressed session record
```

## Quick Start

### 1. During a session (automatic via integration hooks)

The system is designed to be called from `context_manager.py` at key points:

```python
from workspace.session_memory.integration import (
    note_on_chunk_open,
    note_on_chunk_close,
    note_on_tool_call,
    note_on_file_edit,
    note_on_decision,
    note_on_error,
    note_on_finding,
    note_on_question,
)

# In context_manager.open_chunk():
note_on_chunk_open(chunk_id, user_input)

# In context_manager.close_chunk():
tools_used = [extract tool names from _pending_context]
note_on_chunk_close(chunk_id, user_input, assistant_reply, tools_used)

# When agent makes a decision:
note_on_decision(chunk_id, "Chose approach X because Y", "Reasoning here")

# When agent edits a file:
note_on_file_edit(chunk_id, "path/to/file.py", "modify", "Added session memory hooks")

# When an error occurs:
note_on_error(chunk_id, "Permission denied", "Reading api.keys", "User approved")
```

### 2. Manual note-taking (from agent code)

```python
from workspace.session_memory import append_session_note

append_session_note(
    note_type="decision",      # decision, action, finding, file_change, question, error, summary
    content="Chose hybrid approach for session memory",
    chunk_id=42,
    metadata={"reasoning": "Balances incremental capture with cheap finalization"}
)
```

### 3. Finalize a session

```python
from workspace.session_memory import finalize_session

# Uses small model (cohere/north-mini-code:free) for intelligent compression
session_file = finalize_session(use_small_model=True)

# Or just structure the draft as-is (faster, no API call)
session_file = finalize_session(use_small_model=False)
```

### 4. Resume a session

```python
from workspace.session_memory import resume_session, list_sessions

# List all sessions
sessions = list_sessions()
# [{'session_id': '2026-10-23_15-56-07', 'started': '...', 'summary': '...', ...}]

# Resume a specific session
session_data = resume_session("2026-10-23_15-56-07")
# Returns full session dict with steps, decisions, files_modified, etc.
```

## CLI Usage

```bash
# List all sessions
python workspace/session_memory/session_memory.py list

# Resume a session (shows full JSON)
python workspace/session_memory/session_memory.py resume 2026-10-23_15-56-07

# Delete a session
python workspace/session_memory/session_memory.py delete 2026-10-23_15-56-07

# Show current draft
python workspace/session_memory/session_memory.py draft

# Finalize current draft
python workspace/session_memory/session_memory.py finalize
```

## Session File Format

```json
{
  "session_id": "2026-10-23_15-56-07",
  "started": "2026-10-23T15:56:07.123456",
  "ended": "2026-10-23T16:45:12.789012",
  "updated": "2026-10-23T16:45:12.789012",
  "chunk_range": [152, 168],
  "summary": "User asked about session memory; designed session resume file; implemented prototype.",
  "key_outcomes": [
    "Identified gap: no single-session resume artifact",
    "Designed session_<timestamp>.json format",
    "Prototype written to workspace/session_prototype.py"
  ],
  "decisions": [
    "Use existing chunk system as source of truth",
    "Compress via small model (north-mini-code) for cost",
    "Store in logs/ alongside chunks"
  ],
  "open_questions": [
    "Should we auto-save every N turns or only on command?"
  ],
  "files_modified": [
    "core/context_manager.py",
    "workspace/session_memory/session_memory.py"
  ],
  "tools_used": [
    "read_file",
    "write_file",
    "shell",
    "delegate_subtask"
  ],
  "token_estimate": 12000,
  "steps": [
    {
      "ts": "2026-10-23T15:56:07.123456",
      "type": "decision",
      "content": "Chose hybrid draft + small-model compression",
      "chunk_id": 152,
      "metadata": {"reasoning": "Balances incremental capture with cheap finalization"}
    }
    // ... more steps (max 15 after compression)
  ]
}
```

## Note Types

| Type | Purpose | Example |
|------|---------|---------|
| `decision` | A choice made with reasoning | "Chose approach X because Y" |
| `action` | Something the agent did | "Edited core/context_manager.py" |
| `finding` | Discovered fact | "Chunk system has 3-level compression" |
| `file_change` | File created/modified/deleted | "Created workspace/session_memory.py" |
| `question` | Open question | "Should we auto-save every N turns?" |
| `error` | Error + recovery | "Permission denied; user approved" |
| `summary` | High-level phase summary | "Completed session memory prototype" |

## Compression Strategy

- **Draft phase**: Agent appends structured notes incrementally (near-zero overhead)
- **Finalize phase**: Small model (`cohere/north-mini-code:free`, free on OpenRouter, 256k context) compresses draft to ~15 key steps
- **Fallback**: If small model fails, draft is saved as-is

## Integration with context_manager.py

Add to `context_manager.py` imports:
```python
from workspace.session_memory.integration import (
    note_on_chunk_open,
    note_on_chunk_close,
    note_on_tool_call,
    note_on_file_edit,
    note_on_decision,
    note_on_error,
)
```

Call in `open_chunk()`:
```python
note_on_chunk_open(cid, user_input)
```

Call in `close_chunk()`:
```python
tools_used = [m.get("tool_calls", [{}])[0].get("function", {}).get("name", "unknown")
              for m in _pending_context if m.get("role") == "assistant" and m.get("tool_calls")]
note_on_chunk_close(cid, user_input, assistant_reply, tools_used, reasoning_meta)
```

For file edits done via shell/agent, call `note_on_file_edit` directly from the agent code.

## Design Decisions

1. **Hybrid draft + small-model compression** — Incremental capture during session, cheap compression at end
2. **Separate draft file** — Survives crashes; draft is always current
3. **Structured note types** — Easy to filter, search, and compress
4. **Small model for compression** — Free, fast, 256k context, good at JSON
5. **Session files in logs/** — Alongside chunks and summaries, easy to find
6. **Stable session IDs** — Timestamp-based, sortable, human-readable