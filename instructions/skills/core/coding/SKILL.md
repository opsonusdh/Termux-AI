---
name: coding
description: Coding Standards. This is a mandatory Termux-AI operational skill for tasks matching its scope.
---

## Termux-AI integration contract

- This skill is part of the repository's authoritative operating system. Apply it with the selected domain skills, not as an optional suggestion.
- Use the current project files and the tools actually available to Orion. Never invent a connector, package, API, or filesystem capability.
- Higher-priority instructions are the user request and runtime safety/permission controls.
- When this skill conflicts with a more specific task skill, follow the task-specific rule only where the conflict is explicit; preserve repository safety and verification rules.

# Coding Standards

This document defines the coding standards, architectural rules, and safety guidelines for the Orion/Termux-AI codebase. The rules here exist for specific reasons — understanding those reasons is more important than memorizing the rules.

---

## 0. Instruction-Read Gate

This manual is executable policy for coding tasks, not background documentation.

Before any source-changing action:
1. Read `instructions/skills/core/coding/SKILL.md` in the current session with the file tool.
2. Read any additional required manuals identified by `core/PROMPT.md`.
3. Read every target file before editing it.
4. Record the verified target files and applicable manuals in the task scratchpad when the task is multi-step.

If `coding.md` has not been read in the current session, do not write, delete, or run a mutating command.

This gate is intentionally redundant with the system prompt: the prompt selects the policy, while this manual states the policy itself so the rule remains available if the task is resumed or the prompt is compressed.

---

## Before Touching Any File

Read the target file in full before making any modification. This is not optional and not limited to "coding tasks." It applies whenever you intend to write, edit, or delete any file.

**Why:** You cannot safely modify a file whose current state you don't know. Memory of what a file "probably contains" is not the same as seeing it. Files change between sessions. A write based on a stale mental model causes regressions that are hard to trace.

**Pre-edit Analysis:**
Before proposing any code change:
1. **Full File Read:** Always read the *entire* target file to understand its current state, context, and existing patterns.
2. **Dependency Check:** Identify all direct and indirect dependencies of the code you intend to modify. Consider how changes might impact other modules or components.
3. **Exact Line Number Identification:** For targeted edits, pinpoint the precise line numbers where changes need to occur.
4. **Related Files:** If the task involves a new feature or significant change, proactively identify other files (e.g., `paths.py`, `core/tools.py`, `core/llm_client.py`) that might require corresponding updates.

**Action-gated rule:** Before any `write_file`, `run_code` (that writes), or bash command that modifies a file — confirm you have read that file in the current session. If you haven't, read it first.

---

## Recognize the Operation Type Before Starting

Before writing a single line, identify whether this is:

- **A targeted change** — one specific location, one specific edit → read → edit → verify
- **A bulk transformation** — the same change in N places → read → write a script → run once → verify
- **Adding a new component** — new function, new tool, new wrapper → plan the full addition chain first

**The bulk rule:** If the same pattern needs to change in more than three places in a file, do not make three individual edits. Write a Python script or use `sed` to make all changes in one pass. It is faster, more consistent, and does not consume round trips.

---

## 1. Environment Constraints

The system runs **primarily in Termux/Android** with cross-platform compatibility for Linux, macOS, and Windows:

- **No global writable paths.** `/tmp`, `/var`, `/usr/local` are not reliably writable across all platforms. All paths must resolve through `paths.py` under the project root (`~/Termux-AI`).
- **IPC limitations.** Named pipes (FIFOs) and POSIX shared memory are restricted on Android. Use `multiprocessing.Queue` exclusively.
- **Resource pressure.** Keep threads minimal, always join subprocesses, and close file handles in `finally` blocks.
- **Cross-Platform Compatibility.** Never hardcode OS-specific commands without a platform check (`sys.platform == 'win32'`). For Termux, prefer `termux-*` commands via wrappers.

---

## 2. Module Boundaries

Every package has a strict responsibility. Crossing these boundaries causes regressions.

| Package | Responsibility | Import Pattern |
|:---|:---|:---|
| `core/` | LLM inference, tool dispatch, context management, chat loop | flat imports within core (`from tools import *`) |
| `agent/` | Task state, planning, execution, validation | `from agent import state_manager` |
| `orchestration/` | Multi-process delegation, IPC | `from orchestration import Manager, Worker` |
| `reflection/` | Execution logging, failure analysis, auto-retry | `from reflection import ReflectionLoop, attempt_correction` |
| `tools/` | Platform hardware API wrappers only (Termux API) | `from tools import wrapper_termux_battery_status` |
| `config/` | Secrets and runtime settings — no logic | read via `paths.API_KEYS_FILE`, `paths.CONFIG_FILE` |
| `data/` | Persistent state and schemas — no logic | read/write via `agent/state_manager.py` only |

### Absolute Rules

- **Do not wrap or replace `ask_ai()` and do not change its public return contract.** A bug fix inside `ask_ai()` is allowed only when the current task specifically requires it and the affected code is read and tested; prefer helper functions and provider-specific logic outside the public contract.
- **Never write to `data/state.json` directly.** Use `agent/state_manager.save_state()`.
- **Never append chat turns to the session `history` list.** `context_manager.build_history()` serves full conversation history from chunks.
- **Never summarize inside tool execution or between LLM calls.** `maybe_summarize_async()` runs only after `close_chunk()`.

---

## 3. sys.path Bootstrap

Every module that imports across packages must bootstrap `sys.path` the same way — `core/` first, root second:

```python
import os, sys
_CORE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_CORE)
if _CORE not in sys.path: sys.path.insert(0, _CORE)
if _ROOT not in sys.path: sys.path.insert(1, _ROOT)
```

This must appear **before** any project imports.

---

## 4. Centralized Path Resolution (`paths.py`)

`paths.py` is the **single source of truth** for every file path. Import it everywhere:

```python
import paths

state_file    = paths.STATE_FILE
api_keys      = paths.API_KEYS_FILE
config_file   = paths.CONFIG_FILE
logs_dir      = paths.LOGS_DIR
```

---

## 5. Error Handling & Defensive Programming

- **Never swallow exceptions silently.** No `except: pass` or `except Exception: pass` without at minimum a `print` or log entry.
- **Programmatic Exception Diagnosis:** Inspect errors programmatically.
- **Idempotence & Safety:** Every script or background logic block must be safe to execute multiple times.
- **Timeouts and Resource Limits:** When interacting with external systems or long-running operations, implement explicit timeouts.

---

## 6. Concurrency Rules

- **Main inference thread is single.** `ask_ai()` is synchronous.
- **Two background daemon threads permitted:** context summarizer (`maybe_summarize_async`) and reflection logger.
- **Thread-safe state access.** `context_manager.py` uses `_lock` around all reads/writes to shared state.
- **No shared mutable state between processes.** `orchestration/Manager` communicates exclusively through `multiprocessing.Queue`.

---

## 7. Adding a New LLM-Callable Tool

1. Write the implementation function in `core/tools.py`
2. Add the JSON schema to `TOOLS_DESCRIPTION` in `core/llm_client.py`
3. Add the dispatch `lambda` to `_dispatch_tool()` in `core/llm_client.py`
4. If the tool wraps a platform API: create wrapper first in `tools/`, call it from `core/tools.py`
5. Run syntax check on all modified files
6. Write a minimal dispatch test that calls `_dispatch_tool()` and asserts the return value

---

## 8. Testing & Verification

Every change **MUST** be verified:

```bash
# Syntax compilation check
python3 -m py_compile <path_to_file>

# Import check
python3 -c "import sys; sys.path.insert(0,'core'); import tools; print('OK')"
```

If any, **MUST** fix errors that are found.

---

## 9. Python 3.10+ Best Practices

- **Type Hints:** Use explicit typing and `X | Y` for Union types.
- **Pattern Matching:** Use `match ... case` statements where suitable.
- **Context Managers:** Always use `with` statements for resource management.