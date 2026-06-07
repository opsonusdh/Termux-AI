# Agentic Orchestration & Collaboration Workflows

This document covers the multi-process delegation system, the agent execution loop, the reflection pipeline, and the decision-making framework for choosing when to orchestrate vs. execute directly.

---

## 1. When to Orchestrate vs. Execute Directly

Before reaching for the orchestration layer, ask: is delegation actually necessary?

| Situation | Right approach |
|---|---|
| Single file or function change | Execute directly via `run_code` or `write_file` |
| Multi-step task with clear sequence | Execute directly using the reasoning lifecycle in `reasoning.md` |
| Tasks that can run in parallel | Orchestrate via `orchestration/Manager` |
| Tasks requiring isolated environments or separate processes | Orchestrate |
| Tasks you could do yourself in one thread | Do them yourself — orchestration adds overhead |

Orchestration is a tool for parallelism and isolation, not a default for all multi-step work. Unnecessary orchestration adds IPC overhead, error surface area, and debugging complexity.

---

## 2. Architecture Overview

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

For multi-process tasks, `orchestration/Manager` delegates to `Worker` processes over `multiprocessing.Queue`:

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

## 3. Agent Execution Loop (`run_agent_step`)

Location: `core/llm_client.py`

### Task Recovery Priority
When `/agent` is triggered, the supervisor resolves the next task in this order:

1. `active_task_id` — interrupted mid-execution; resume it
2. `cursor` — last known position; pick up from there
3. First `pending` or `active` task — fallback for fresh starts

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
The retry executes **immediately** in the same call. `retry_count` is an integer field on the subtask.

### State Persistence Contract
- `worker_output` is written to disk **before** the critic call.
- `critic_output` is written to disk **before** any status update.
- A restart at any point can resume from `active_task_id`.

---

## 4. Worker Abstraction (`orchestration/worker.py`)

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

## 5. IPC Lifecycle (`orchestration/protocol.py` + `manager.py`)

Strict sequence — do not deviate:

1. `Manager.load_tasks([...])` — loads and normalises the task list.
2. For each task:
   - Spawn `multiprocessing.Process(target=_run_worker_process, args=(queue, ...))`.
   - `process.start()`
   - `IPCProtocol.receive_status(timeout=30)` — blocks until the worker puts to queue.
   - `process.join()` — always join, even after receiving status.
   - If status is not `success`/`completed`, abort remaining tasks.
3. `Manager.run_all()` returns `{"status": "success"|"failed", "tasks": [...], "history": [...]}`.

Never use FIFOs or shared memory.

---

## 6. Reflection Pipeline (`reflection/`)

Every plan executed through `agent/executor.py` is automatically recorded:

```python
from reflection import ReflectionLoop, attempt_correction

ReflectionLoop.record(plan, result, success=True)
entry    = ReflectionLoop.latest_entry()   # dict or None
diagnosis = ReflectionLoop.analyze()       # runs Reflector on latest entry
outcome  = attempt_correction()            # {"attempted": bool, "result": dict | None, "reason": str}
```

Log file: `logs/reflection.jsonl` (append-only, one JSON object per line).

### When to Consult the Reflection Log

Before retrying a failing operation, check `ReflectionLoop.latest_entry()`. If the same failure appears in recent entries, there is a systematic cause — fix the root problem rather than retrying the same approach.

---

## 7. Capability Registration

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

2. Implement the logic in the appropriate module.
3. If a new LLM-callable tool is needed, follow the full addition chain in `coding.md` section 7.
4. Run an integration test that exercises the full delegation + IPC + teardown chain.

---

## 8. Prohibited Patterns

| Pattern | Reason |
|---|---|
| `subprocess.run(cmd, shell=True)` for validation | Security risk |
| `if "some string" in output: mark_completed()` | Presence of a string is not proof of success |
| Wrapping `ask_ai()` | Breaks tool loop, key rotation, and rate-limit handling |
| State injection into `ask_ai()` system prompt | Agent state belongs only in `run_agent_step()` |
| Orchestrating tasks that could run sequentially in one thread | Unnecessary complexity — prove the flat loop works first |
| Retrying a failing approach without consulting reflection log | Systematic failures require root cause analysis, not more retries |
