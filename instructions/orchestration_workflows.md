# Agentic Orchestration & Collaboration Workflows

This document covers the multi-process delegation system in `orchestration/`, the agent execution loop in `agent/`, and the reflection pipeline in `reflection/`.

---

## 1. Architecture Overview

```
                     ┌─────────────────────┐
                     │   interface.py      │
                     │   /agent trigger    │
                     └──────────┬──────────┘
                                │
                     ┌──────────▼──────────┐
                     │  run_agent_step()   │  ← llm_client.py
                     │  Supervisor loop    │
                     └──┬──────────────┬───┘
                        │              │
               ┌────────▼───┐    ┌─────▼──────┐
               │  Worker    │    │   Critic   │
               │  ask_ai()  │    │  ask_ai()  │
               └────────┬───┘    └─────┬──────┘
                        │              │
               ┌────────▼──────────────▼──────┐
               │      agent/state_manager     │
               │  persist outputs, advance    │
               │  cursor, write state.json    │
               └──────────────────────────────┘
```

For multi-process tasks, `orchestration/Manager` delegates to `Worker` processes over a `multiprocessing.Queue`:

```
orchestration/Manager
    ├── spawns → Worker A (shell)
    ├── spawns → Worker B (python)
    └── spawns → Worker C (mock)
              ↓ writes to
    multiprocessing.Queue  (IPCProtocol)
              ↓ reads from
    Manager (collects results, aborts on failure)
```

---

## 2. Agent Execution Loop (`run_agent_step`)

Location: `core/llm_client.py`

### Task Recovery Priority
When `/agent` is triggered, the supervisor resolves the next task in this order:

1. `active_task_id` — a task was interrupted mid-execution; resume it.
2. `cursor` — last known position; pick up from there.
3. First `pending` or `active` task — fallback for fresh starts or corrupt cursors.

### Execution Flow (one step)
```
resolve task
    ↓
mark status="active", persist worker_output="" 
    ↓
Worker: ask_ai(worker_prompt)
    ↓
persist worker_output to state.json
    ↓
Critic: ask_ai(critic_prompt)
    ↓
persist critic_output to state.json
    ↓
"VERIFIED" → mark completed, advance cursor
"FAILED"   → retry_count < 1 → run Worker again → run Critic again
           → retry_count ≥ 1 → mark failed, advance cursor
```

### One-Retry Rule
The retry executes **immediately** in the same call — it does not return and wait for another `/agent` trigger. `retry_count` is an integer field on the subtask, not a string flag.

### State Persistence Contract
- `worker_output` is written to disk **before** the critic call.
- `critic_output` is written to disk **before** any status update.
- A restart at any point can resume from `active_task_id`.

---

## 3. Worker Abstraction (`orchestration/worker.py`)

Task input schema:

```json
{
  "id":          "task_01",
  "worker_name": "SystemCheck",
  "type":        "shell",
  "command":     "termux-battery-status"
}
```

Supported types:

| Type | Behaviour |
|---|---|
| `shell` | Runs via `bash -c <command>` |
| `python` | Runs as `python3 <file>` or `python3 -c <code>` |
| `mock` | Returns `mock_response` dict immediately, no subprocess |

Output schema (reported via `multiprocessing.Queue`):

```json
{
  "worker":     "SystemCheck",
  "task_type":  "shell",
  "status":     "success",
  "returncode": 0,
  "stdout":     "...",
  "stderr":     "",
  "duration":   0.24
}
```

Valid statuses: `success`, `failed`, `error`.

---

## 4. IPC Lifecycle (`orchestration/protocol.py` + `manager.py`)

Strict sequence — do not deviate:

1. `Manager.load_tasks([...])` — loads and normalises the task list.
2. For each task:
   - Spawn `multiprocessing.Process(target=_run_worker_process, args=(queue, ...))`.
   - `process.start()`
   - `IPCProtocol.receive_status(timeout=30)` — blocks until the worker puts to queue.
   - `process.join()` — always join, even after receiving status.
   - If status is not `success`/`completed`, abort remaining tasks.
3. `Manager.run_all()` returns `{"status": "success"|"failed", "tasks": [...], "history": [...]}`.

Never use FIFOs or shared memory — Android's security sandbox restricts them. Always use `multiprocessing.Queue`.

---

## 5. Reflection Pipeline (`reflection/`)

Every plan executed through `agent/executor.py` is automatically recorded:

```python
from reflection import ReflectionLoop, attempt_correction

# Record an outcome
ReflectionLoop.record(plan, result, success=True)

# Read the most recent entry
entry = ReflectionLoop.latest_entry()   # dict or None

# Analyse failure
diagnosis = ReflectionLoop.analyze()   # runs Reflector on latest entry

# Auto-retry if last result was Failure
outcome = attempt_correction()
# {"attempted": bool, "result": dict | None, "reason": str}
```

Log file: `logs/reflection.jsonl` (append-only, one JSON object per line).

---

## 6. Capability Registration (`config/capability_registry.json`)

When adding a new capability:

1. Add an entry to `config/capability_registry.json`:

```json
{
  "name":        "my_new_capability",
  "module":      "orchestration.worker",
  "function":    "Worker",
  "description": "What this does."
}
```

2. Implement the logic in the appropriate module (`orchestration/worker.py` for new task types, `agent/executor.py` for new execution wrappers).

3. If a new LLM-callable tool is needed, add:
   - A function in `core/tools.py`
   - A JSON schema entry in `TOOLS_DESCRIPTION` in `core/llm_client.py`
   - A dispatch case in `_dispatch_tool()` in `core/llm_client.py`

4. Run an integration test that exercises the full delegation + IPC + teardown chain before merging.

---

## 7. Prohibited Patterns

| Pattern | Reason |
|---|---|
| `subprocess.run(cmd, shell=True)` for validation | Security — arbitrary shell execution as a validation mechanism |
| `if "some string" in output: mark_completed()` | Not validation — presence of a string is not proof of success |
| Wrapping `ask_ai()` | Breaks tool loop, key rotation, and rate-limit handling |
| State injection into `ask_ai()` system prompt | Violates normal chat isolation — agent state belongs only in `run_agent_step()` |
| Adding orchestration layers before proving minimal system | Technical debt — prove the flat loop works first |
