# Coding Instructions & Standards

Official coding standards, architectural rules, and safety guidelines for all development within the Orion/Termux-AI codebase.

---

## 1. Environment Constraints

Termux is a sandboxed Linux-like environment on Android with specific restrictions:

- **No global writable paths.** `/tmp`, `/var`, `/usr/local` are not reliably writable. All paths must resolve through `paths.py` under `~/Termux-AI`.
- **IPC limitations.** Named pipes (FIFOs) and POSIX shared memory are restricted by Android's security model. Use `multiprocessing.Queue` exclusively.
- **Resource pressure.** Mobile CPUs throttle aggressively. Keep threads minimal, always join subprocesses, and close file handles in `finally` blocks.
- **No root assumed.** Never require root, SELinux policy changes, or kernel modules.

---

## 2. Module Boundaries

Every package has a strict responsibility. Crossing these boundaries causes regressions.

| Package | Responsibility | Import Pattern |
|---|---|---|
| `core/` | LLM inference, tool dispatch, context management, chat loop | flat imports within core (`from tools import *`) |
| `agent/` | Task state, planning, execution, validation | `from agent import state_manager` |
| `orchestration/` | Multi-process delegation, IPC | `from orchestration import Manager, Worker` |
| `reflection/` | Execution logging, failure analysis, auto-retry | `from reflection import ReflectionLoop, attempt_correction` |
| `tools/` | Termux hardware API wrappers only | `from tools import wrapper_termux_battery_status` |
| `config/` | Secrets and runtime settings — no logic | read via `paths.API_KEYS_FILE`, `paths.CONFIG_FILE` |
| `data/` | Persistent state and schemas — no logic | read/write via `agent/state_manager.py` only |

### Hard Rules
- **Never modify `ask_ai()`** — it is the production inference path. No wrapping, no injection, no return-type changes.
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

This must appear **before** any project imports. Putting it after causes import errors that are difficult to trace.

Why `core/` first: `import tools` must resolve to `core/tools.py` (all LLM-callable tool implementations), not to `tools/` (Termux API wrappers). These are different modules with different purposes.

---

## 4. Centralized Path Resolution (`paths.py`)

`paths.py` at the project root is the **single source of truth** for every file path. Import it everywhere:

```python
import paths

# Use named constants — never construct paths manually
state_file    = paths.STATE_FILE             # data/state.json
api_keys      = paths.API_KEYS_FILE          # config/api.keys
config_file   = paths.CONFIG_FILE            # config/config.json
logs_dir      = paths.LOGS_DIR              # logs/
chunks_file   = paths.CHUNKS_FILE           # logs/chunks.jsonl
summaries     = paths.CHUNK_SUMMARIES_FILE  # logs/chunk_summaries.json
reflection    = paths.REFLECTION_LOG_FILE   # logs/reflection.jsonl

# Build sub-paths from directory constants
battery_tool  = os.path.join(paths.TOOLS_DIR, "wrapper_termux_battery_status.py")
```

**Never** construct paths with string literals, `os.getcwd()`, or `~`:
```python
# Wrong
state = "~/Termux-AI/data/state.json"
state = os.path.join(os.getcwd(), "data", "state.json")
```

---

## 5. Error Handling

- **Never swallow exceptions silently.** No `except: pass` or `except Exception: pass` without at minimum a `print` or log entry.
- **Structured fallbacks.** Hardware failures (battery check, Wi-Fi scan) must return a clean `{"status": "error", "error_type": "...", "message": "..."}` dict, not raise.
- **Background threads must not crash silently.** The summarizer and reflection logger must wrap their work in `try/except` and exit cleanly on failure — they must never propagate exceptions to the main thread.
- **Summarizer failures are non-fatal.** If `_call_summarizer()` returns `""`, the chunk simply has no summary yet. This is acceptable — the pending-summary snippet path in `build_history()` handles it.

---

## 6. Concurrency Rules

- **Main inference thread is single.** `ask_ai()` is synchronous. Only one inference call runs at a time.
- **Background daemon threads** — currently two permitted: the context summarizer (`maybe_summarize_async`) and any reflection logger. Both are daemon threads (they die with the process).
- **Thread-safe state access.** `context_manager.py` uses `_lock` around all reads/writes to `_parent_ids`, `_chunk_index`, `_summaries`, and `_current_chunk`. Always acquire the lock when accessing these.
- **No shared mutable state between processes.** `orchestration/Manager` communicates exclusively through `multiprocessing.Queue`. Workers receive their entire task as a JSON argument and write their entire result to the queue.

---

## 7. Adding a New LLM-Callable Tool

1. Write the implementation in `core/tools.py`.
2. Add the JSON schema to `TOOLS_DESCRIPTION` in `core/llm_client.py`.
3. Add the dispatch `lambda` to `_dispatch_tool()` in `core/llm_client.py`.
4. If the tool wraps a Termux API: create `tools/wrapper_<name>.py` first, call it from `core/tools.py`.
5. Run a syntax check on both files. Write a minimal test that dispatches the tool through `_dispatch_tool()` and asserts the return value.

---

## 8. Testing & Verification

Every change must be verified before it is considered done:

```python
# Minimum syntax check after any edit
python3 -c "import ast; ast.parse(open('core/context_manager.py').read())"

# Minimum runtime check after a refactor
python3 -c "
import sys; sys.path.insert(0,'core'); sys.path.insert(1,'.')
import context_manager as cm
cm.open_chunk('test'); cm.set_tool_context([]); cid = cm.close_chunk('reply')
assert cid == 1
print('OK')
"
```

Integration tests must:
1. Cover the full call chain (e.g., `open_chunk → set_tool_context → close_chunk → build_history → retrieve_chunk`).
2. Assert specific values, not just "no exception".
3. Test edge cases: empty history, missing summary, subchunk retrieval, disk round-trip.

Write tests as inline Python scripts in `workspace/` during development; delete them when done.
