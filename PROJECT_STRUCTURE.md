# Termux-AI Project Structure

Run the assistant: `python core` from the project root.

---

## Directory Layout

```
Termux-AI/
│
├── paths.py                    ← Single source of truth for ALL file paths.
│                                 Every module imports from here. Never hardcode.
│
├── setup.sh                    ← Fresh-install bootstrap script (pkg + pip)
├── PROJECT_STRUCTURE.md        ← This file
│
├── core/                       ← Main runtime engine
│   ├── __main__.py             ← Entry point  (python core  or  python -m core)
│   ├── interface.py            ← Chat loop, /agent trigger, STT/TTS wiring
│   ├── llm_client.py           ← ask_ai(), multi-provider routing, key rotation,
│   │                             tool dispatch, run_agent_step()
│   ├── context_manager.py      ← Two-layer chunk memory (raw store + active window)
│   ├── tools.py                ← All LLM-callable tools, build_memory_block(),
│   │                             ask_ai_simple(), run_diagnosis(), sleep_mode()
│   ├── prompt.py               ← SYSTEM_PROMPT constant
│   ├── renderer.py             ← Markdown → terminal, TTS render helpers, ANSI colors
│   ├── permissions.py          ← Command safety validation (validate_command)
│   └── whatsapp_manager.py     ← WhatsApp bridge integration
│
├── agent/                      ← Planning, execution, validation, state
│   ├── __init__.py
│   ├── state_manager.py        ← Project/task CRUD, cursor, crash recovery,
│   │                             checkpoint writing, persona management
│   ├── planner.py              ← create_plan() / commit_plan() scaffolding
│   ├── executor.py             ← Post-execution wrapper: validates + records to reflection
│   └── validator.py            ← JSON-schema validation of execution results
│
├── orchestration/              ← Multi-process task delegation
│   ├── __init__.py             ← Exports: Orchestrator, Manager, Worker, IPCProtocol
│   ├── orchestrator.py         ← High-level subprocess delegator (spawns worker scripts)
│   ├── manager.py              ← Sequential multi-worker task manager over IPC
│   ├── worker.py               ← Task execution: shell / python / mock
│   └── protocol.py             ← multiprocessing.Queue IPC wrapper
│
├── reflection/                 ← Self-diagnosis and correction
│   ├── __init__.py             ← Exports: ReflectionLoop, attempt_correction
│   │                             ReflectionLoop.record() / .latest_entry() / .analyze()
│   ├── reflector.py            ← Failure analyser: produces diagnosis + suggested fix
│   └── self_correction.py      ← Reads reflection log, re-runs failed plans
│
├── tools/                      ← Termux API wrapper package (hardware/OS bindings)
│   ├── __init__.py             ← Package marker (kept minimal — NOT core/tools.py)
│   ├── tool_wrappers.py        ← Generic wrapper base classes
│   ├── wrapper_termux_battery_status.py
│   └── wrapper_termux_wifi_scaninfo.py
│
├── config/                     ← Secrets and runtime config (gitignored)
│   ├── api.keys                ← JSON: {"google": [...], "nvidia": [...], "groq": [...]}
│   ├── config.json             ← {"stt_path": "...", "tts_enabled": false, ...}
│   └── capability_registry.json← Module/function registry for registered capabilities
│
├── data/                       ← Persistent state and schemas
│   ├── state.json              ← Live agent state (gitignored, written by state_manager)
│   └── validator_schema.json   ← JSON schema for execution result validation
│
├── logs/                       ← All log files (gitignored)
│   ├── chunks.jsonl            ← Raw conversation chunk store (append-only)
│   ├── chunk_summaries.json    ← Progressive summaries keyed by chunk ID
│   ├── reflection.jsonl        ← Reflection loop records (plan→result→success)
│   └── history.jsonl           ← Legacy turn log (retained for compatibility)
│
├── instructions/               ← Agent system instructions (markdown)
│   ├── readme.md               ← Index of all instruction manuals
│   ├── coding.md               ← Coding standards, paths.py usage, error handling
│   ├── reasoning.md            ← Task decomposition and troubleshooting patterns
│   ├── orchestration_workflows.md ← Worker lifecycle, IPC protocol, delegation
│   └── environment_and_tools.md   ← Termux API, security boundaries, wrapper pattern
│
├── workspace/                  ← Scratch space for agent-generated files (gitignored)
│   └── morning_report.py       ← Example agent-generated utility script
│
└── docs/
    └── patches/                ← Historical patch files and reasoning artifacts
```

---

## Sys.path Convention

Every module sets up sys.path the same way — **core/ first, root second**:

```python
_CORE = os.path.dirname(os.path.abspath(__file__))   # wherever this file lives
_ROOT = os.path.dirname(_CORE)                        # project root
if _CORE not in sys.path: sys.path.insert(0, _CORE)
if _ROOT not in sys.path: sys.path.insert(1, _ROOT)
```

This ensures:
- `import tools` → resolves to `core/tools.py` (not `tools/` package)
- `import paths` → resolves to `paths.py` at project root
- `from agent import state_manager` → resolves via root
- `from orchestration import Manager` → resolves via root

---

## Key Data Flows

### Normal chat turn
```
interface.py
  → cm.open_chunk(user_input)
  → cm.build_history()  → [summaries of old chunks] + [raw recent chunks]
  → ask_ai(prompt, history=...)
      → tools.build_memory_block()  → RAG on memories.txt / indexed_memory.txt
      → LLM call (Google / Nvidia / Groq)  →  tool loop  →  final reply
  → cm.close_chunk(reply)
  → cm.maybe_summarize_async()   ← background thread, post-reply only
```

### Agent step  (/agent trigger)
```
interface.py → llm_client.run_agent_step()
  Supervisor: state_manager  →  resolve next task (active_task_id → cursor → first pending)
  Worker:     ask_ai(worker_prompt)  →  persist worker_output
  Critic:     ask_ai(critic_prompt)  →  persist critic_output
  If FAILED and retry_count < 1:
    retry: ask_ai again  →  critic again  →  mark completed or failed
  state_manager.update_subtask(...)  →  _update_cursor_and_active_task(advance=True)
```

### Reflection + self-correction
```
agent.executor.execute_plan(plan)
  → agent.validator.validate_execution(result)
  → reflection.ReflectionLoop.record(plan, result, success)

reflection.attempt_correction()
  → ReflectionLoop.latest_entry()
  → if result.validation == 'Failure':
      → agent.executor.execute_plan(plan)   ← re-runs
      → ReflectionLoop.record(plan, new_result, ...)
```

### Orchestrated multi-task run
```
orchestration.Manager.load_tasks([...])
orchestration.Manager.run_all()
  for each task:
    → spawn multiprocessing.Process(target=Worker.execute_task)
    → IPCProtocol.receive_status(timeout=30)
    → record result, abort on failure
```

---

## Agent Mode Commands

| Command | Behaviour |
|---------|-----------|
| `/agent` | Run one agent step (Supervisor → Worker → Critic, one task) |
| `/agent auto` | Run agent steps in a loop until no pending tasks or failure |

Agent state lives in `data/state.json`. Initialize a project and add subtasks via
the `initialize_project` and `add_subtask` LLM tools (available to the model during chat).

---

## Adding a New Tool

1. Write the Python function in `core/tools.py`.
2. Add the JSON schema entry to `TOOLS_DESCRIPTION` in `core/llm_client.py`.
3. Add the dispatch case to `_dispatch_tool()` in `core/llm_client.py`.
4. If it needs a Termux hardware wrapper: add `tools/wrapper_<name>.py` and expose
   via `tools/__init__.py`.

## Adding a New Orchestration Capability

1. Add the function/class entry to `config/capability_registry.json`.
2. Implement any new worker logic in `orchestration/worker.py` under a new `task_type`.
3. If a standalone worker script is needed, create it in `orchestration/` — it must
   accept a JSON string as `sys.argv[1]` and write its result to the IPC queue.
