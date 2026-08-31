# Termux-AI Project Structure

Run the assistant: `python core` from the project root.

---

## Directory Layout

```
Termux-AI/
│
├── paths.py                    ← Single source of truth for ALL file paths.
│                                 Every module imports from here. Never hardcode.
├── setup.sh                    ← Fresh-install bootstrap script (pkg + pip)
├── PROJECT_STRUCTURE.md        ← This file
├── memories.txt                ← Stored long-term facts, preferences, and instructions
├── indexed_memory.txt          ← RAG indexed code and document search chunks
│
├── core/                       ← Main runtime engine
│   ├── __main__.py             ← Entry point (python core or python -m core)
│   ├── interface.py            ← Chat loop, /agent trigger, STT/TTS wiring
│   ├── llm_client.py           ← ask_ai(), multi-provider routing, key rotation,
│   │                             tool dispatch, run_agent_step()
│   ├── context_manager.py      ← Two-layer chunk memory (raw store + active window)
│   ├── tools.py                ← All LLM-callable tools, build_memory_block(),
│   │                             ask_ai_simple(), run_diagnosis(), sleep_mode()
│   ├── PROMPT.md               ← SYSTEM_PROMPT source of truth
│   ├── display_state.py        ← Visual status display, banners, and task formatting
│   ├── input_handler.py        ← Terminal input reading and slash-command parsing
│   ├── renderer.py             ← Markdown → terminal, TTS render helpers, ANSI colors
│   ├── permissions.py          ← Command safety validation (validate_command)
│   └── whatsapp_manager.py     ← WhatsApp bridge integration manager
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
│   ├── orchestrator.py         ← High-level subprocess delegator
│   ├── manager.py              ← Sequential multi-worker task manager over IPC
│   ├── worker.py               ← Task execution: shell / python / mock
│   └── protocol.py             ← multiprocessing.Queue IPC wrapper
│
├── reflection/                 ← Self-diagnosis and correction
│   ├── __init__.py             ← Exports: ReflectionLoop, attempt_correction
│   ├── reflector.py            ← Failure analyser: produces diagnosis + suggested fix
│   └── self_correction.py      ← Reads reflection log, re-runs failed plans
│
├── tools/                      ← Termux API hardware/OS wrappers
│   ├── __init__.py             ← Package marker
│   ├── tool_wrappers.py        ← Base wrapper abstractions & command runners
│   ├── wrapper_termux_battery_status.py
│   ├── wrapper_termux_brightness.py
│   ├── wrapper_termux_clipboard.py
│   ├── wrapper_termux_location.py
│   ├── wrapper_termux_sms.py
│   ├── wrapper_termux_telephony.py
│   ├── wrapper_termux_torch.py
│   ├── wrapper_termux_vibrate.py
│   ├── wrapper_termux_volume.py
│   └── wrapper_termux_wifi_scaninfo.py
│
├── config/                     ← Secrets and runtime config (gitignored secrets)
│   ├── api.keys                ← JSON API keys for providers
│   ├── api.keys.template       ← Template for API key configuration
│   ├── config.json             ← Runtime settings (STT path, TTS toggles, models)
│   ├── capability_registry.json← Registry for executable sub-AI capabilities
│   └── whatsapp_filters.json   ← Contact/group filtering rules for WhatsApp
│
├── data/                       ← Persistent state and schemas
│   ├── state.json              ← Live agent state (written by state_manager)
│   ├── validator_schema.json   ← JSON schema for execution result validation
│   └── cli_history             ← Terminal prompt command history
│
├── logs/                       ← Execution and chat logs
│   ├── chunks.jsonl            ← Raw conversation chunk store (append-only)
│   ├── chunk_summaries.json    ← Progressive summaries keyed by chunk ID
│   ├── reflection.jsonl        ← Reflection loop records
│   ├── whatsapp_log.jsonl      ← WhatsApp interaction history
│   └── history.jsonl           ← Turn history log
│
├── instructions/               ← System instruction manuals & modular skills
│   ├── readme.md               ← Instruction manual index
│   ├── coding.md               ← Coding guidelines and architecture rules
│   ├── communication.md        ← User tone, style, and formatting rules
│   ├── decision_making.md     ← Task prioritization and decision logic
│   ├── environment_and_tools.md← Termux API and tool execution principles
│   ├── memory_and_context.md   ← Memory management and context window rules
│   ├── orchestration_workflows.md ← Delegated task workflows & IPC patterns
│   ├── reasoning.md            ← Problem solving and diagnostic workflows
│   ├── release_and_maintenance.md ← Code updating and versioning standards
│   ├── security_and_privacy.md ← Credential protection and safe execution
│   ├── skills_manifest.json    ← Manifest listing installed skills
│   ├── tool_efficiency.md      ← Optimal tool choice and call minimisation
│   ├── troubleshooting.md      ← System diagnosis and issue resolution
│   ├── verification.md         ← Plan verification and outcome testing
│   ├── whatsapp_and_notifications.md ← Auto-reply and messaging behavior
│   └── skills/                 ← Modular skill extensions
│       ├── INDEX.md            ← Skill repository index
│       ├── core/               ← Native platform skills
│       └── imported/           ← Specialist capabilities (docx, pptx, pdf, artifacts, design, etc.)
│
├── Termux-WP/                  ← WhatsApp bot bridge (Node.js whatsapp-web.js client)
│   ├── main.py                 ← WhatsApp bridge interface python runner
│   ├── bot.js                  ← Headless Chrome / WebJS client handler
│   └── package.json            ← Node dependencies for WhatsApp client
│
├── Termux-STT/                 ← Speech-to-Text integration (whisper.cpp engine)
│   ├── main.py                 ← Local STT audio recorder & whisper handler
│   ├── download_model.sh       ← Helper script to fetch whisper models
│   └── whisper.cpp/            ← Embedded whisper C++ inference codebase
│
├── workspace/                  ← Scratch space for agent execution & generated artifacts
└── docs/                       ← Documentation and historical patches
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

### Agent step (/agent trigger)
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
## Commands & Controls

| Command | Behaviour |
|---------|-----------|
| `/autonomous on` | Enable autonomous mode (bypasses permission prompts for shell commands) |
| `/autonomous off` | Disable autonomous mode (restores interactive permission prompts) |
| `/expand` / `/collapse` | Toggle detailed tool/reasoning outputs in the terminal |
| `/agent` | Run one agent step (Supervisor → Worker → Critic, one task) |
| `/agent auto` | Run agent steps in a loop until no pending tasks or failure |

Agent state lives in `data/state.json`. Initialize a project and add subtasks via
the `initialize_project` and `add_subtask` LLM tools.

Agent state lives in `data/state.json`. Initialize a project and add subtasks via
the `initialize_project` and `add_subtask` LLM tools.
