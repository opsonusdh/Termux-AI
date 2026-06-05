# Termux-AI — Orion

A modular, self-correcting autonomous AI agent for Termux. Orion runs entirely on-device, uses free cloud LLM APIs for inference, and exposes a terminal chat loop with tool use, persistent memory, voice I/O, chunk-based context compression, and a full agentic execution layer.

---

## Features

- **Multi-provider LLM fallback** — Cycles through Google Gemini, Groq, and NVIDIA models automatically; rotates API keys on rate-limits.
- **Tool use** — `run_code`, `read_file`, `write_file`, `web_scrape`, `save_memory`, `retrieve_memory`, `index_files`, `intermediate_print`, `sleep_mode`.
- **Chunk-based context memory** — Conversation history is divided into stable numbered chunks. Old chunks are progressively compressed (short → micro → one-line summary) in a background thread. Raw chunks are permanently stored and retrievable by ID via `retrieve_chunk` / `list_chunks` tool calls.
- **Persistent memory** — Two-tier RAG system: personal facts (`memories.txt`) and indexed code/docs (`indexed_memory.txt`).
- **Agentic execution** — `/agent` triggers a Supervisor → Worker → Critic loop. Tasks are planned via `agent/planner.py`, executed with one retry, and persisted across restarts through `data/state.json`.
- **Orchestration** — `orchestration/` provides multi-process task delegation (`Manager` → `Worker`) over a `multiprocessing.Queue` IPC channel.
- **Self-correction** — `reflection/` logs every execution outcome and automatically retries failures via `attempt_correction()`.
- **Voice I/O** — Optional STT via [Termux-STT](https://github.com/opsonusdh/Termux-STT) and TTS via `edge-tts` + `mpv`.
- **WhatsApp integration** — Send/receive messages and enable busy mode via [Termux-WP](https://github.com/opsonusdh/Termux-WP).
- **Safe execution** — `permissions.py` validates every shell command before dispatch.

---

## Requirements

| Dependency | Install |
|---|---|
| Python 3.10+ | `pkg install python` |
| Rust / cmake / clang | `pkg install rust cmake clang which` |
| `openai` SDK | `pip install openai` |
| `beautifulsoup4` | `pip install beautifulsoup4` |
| `requests` | `pip install requests` |
| `jsonschema` | `pip install jsonschema` |
| **Voice (optional)** | |
| `edge-tts` | `pip install edge-tts` |
| `mpv` | `pkg install mpv` |
| Termux-STT | See [Termux-STT repo](https://github.com/opsonusdh/Termux-STT) |
| **WhatsApp (optional)** | |
| Node.js | `pkg install nodejs` |
| Termux-WP | See [Termux-WP repo](https://github.com/opsonusdh/Termux-WP) |

---

## Setup

```bash
# 1. Clone
git clone https://github.com/opsonusdh/Termux-AI ~/Termux-AI
cd ~/Termux-AI

# 2. Install dependencies
bash setup.sh

# 3. Add API keys
nano config/api.keys
```

`config/api.keys` must be valid JSON:

```json
{
  "google": ["YOUR_GEMINI_KEY_1", "YOUR_GEMINI_KEY_2"],
  "groq":   ["YOUR_GROQ_KEY"],
  "nvidia": ["YOUR_NVIDIA_KEY"]
}
```

```bash
# 4. Run
python core
```

Free API keys: [Google AI Studio](https://aistudio.google.com/) · [Groq Console](https://console.groq.com/) · [NVIDIA NIM](https://build.nvidia.com/)

---

## Directory Structure

```
~/Termux-AI/
│
├── paths.py                        ← Single source of truth for all file paths
├── setup.sh                        ← Dependency installer
├── PROJECT_STRUCTURE.md            ← Full architecture reference
│
├── core/                           ← Main runtime engine
│   ├── __main__.py                 ← Entry point  (python core)
│   ├── interface.py                ← Chat loop, /agent trigger, voice I/O
│   ├── llm_client.py               ← Multi-provider LLM client, tool dispatch,
│   │                                  run_agent_step()
│   ├── context_manager.py          ← Chunk-based two-layer memory system
│   ├── tools.py                    ← All LLM-callable tool implementations
│   ├── prompt.py                   ← System prompt
│   ├── renderer.py                 ← Terminal markdown renderer, ANSI colours
│   ├── permissions.py              ← Shell command safety validator
│   └── whatsapp_manager.py         ← WhatsApp bridge
│
├── agent/                          ← Planning, execution, validation, state
│   ├── state_manager.py            ← Task CRUD, cursor, crash recovery,
│   │                                  checkpoint writing, persona management
│   ├── planner.py                  ← create_plan() / commit_plan()
│   ├── executor.py                 ← Post-execution wrapper (validates + logs)
│   └── validator.py                ← JSON-schema validation of results
│
├── orchestration/                  ← Multi-process task delegation
│   ├── orchestrator.py             ← Subprocess delegator
│   ├── manager.py                  ← Sequential multi-worker task manager
│   ├── worker.py                   ← Task execution: shell / python / mock
│   └── protocol.py                 ← multiprocessing.Queue IPC wrapper
│
├── reflection/                     ← Self-diagnosis and correction
│   ├── __init__.py                 ← ReflectionLoop, attempt_correction
│   ├── reflector.py                ← Failure analyser
│   └── self_correction.py          ← Auto-retry on validation failure
│
├── tools/                          ← Termux hardware API wrappers
│   ├── tool_wrappers.py
│   ├── wrapper_termux_battery_status.py
│   └── wrapper_termux_wifi_scaninfo.py
│
├── instructions/                   ← Agent-facing operational manuals
│   ├── readme.md                   ← Manual index
│   ├── coding.md                   ← Standards, paths.py, error handling
│   ├── reasoning.md                ← Task decomposition, troubleshooting
│   ├── orchestration_workflows.md  ← Worker lifecycle, IPC protocol
│   └── environment_and_tools.md    ← Termux API, security, wrapper pattern
│
├── config/                         ← Secrets and runtime config (gitignored)
│   ├── api.keys                    ← {"google":[...], "groq":[...], "nvidia":[...]}
│   ├── config.json                 ← {"stt_path":"...", "tts_enabled":false}
│   └── capability_registry.json    ← Registered module/function capabilities
│
├── data/                           ← Persistent state and schemas
│   ├── state.json                  ← Live agent state (gitignored)
│   └── validator_schema.json       ← JSON schema for execution results
│
├── logs/                           ← All log files (gitignored)
│   ├── chunks.jsonl                ← Raw conversation chunk store (append-only)
│   ├── chunk_summaries.json        ← Progressive summaries by chunk ID
│   ├── reflection.jsonl            ← Execution outcome log
│   └── history.jsonl               ← Legacy turn log
│
├── workspace/                      ← Scratch space for agent tasks (gitignored)
│   └── morning_report.py
│
└── docs/
    └── patches/                    ← Historical patch files
```

---

## Agent Mode

```
YOU > /agent          # run one step: resolve next task → worker → critic
YOU > /agent auto     # loop until no pending tasks or a failure
```

Initialize a project and add tasks through normal chat — Orion uses the `initialize_project` and `add_subtask` tools. State persists in `data/state.json` and survives restarts.

---

## Context Memory

Every conversation turn is stored as a numbered chunk in `logs/chunks.jsonl`. The active context window always stays small:

```
[system] Chunk 1: <one-line summary>
[system] Chunk 2: <micro summary>
[system] Chunk 3: <short summary>
[user / assistant / tool]  ← raw chunk N-1
[user / assistant / tool]  ← raw chunk N  (most recent)
```

Older chunks are compressed progressively in a background thread. The model can call `list_chunks` and `retrieve_chunk` to pull full raw history when needed.

---

## Voice Mode

Requires [Termux-STT](https://github.com/opsonusdh/Termux-STT), `edge-tts`, and `mpv`.

```bash
cd ~/Termux-AI
git clone https://github.com/opsonusdh/Termux-STT
cd Termux-STT && bash setup.sh
```

```
YOU > start voice     # switch to voice input + TTS output
YOU > stop voice      # switch back to keyboard
```

Set `"tts_enabled": true` in `config/config.json` to start in voice mode by default.

---

## WhatsApp Integration

Requires [Termux-WP](https://github.com/opsonusdh/Termux-WP) and Node.js.

```bash
cd ~/Termux-AI
git clone https://github.com/opsonusdh/Termux-WP
cd Termux-WP && bash setup.sh
```

```
YOU > Enable busy mode on WhatsApp for the next hour.
```

---

## api.keys Format

Multiple keys per provider are rotated round-robin and retired on rate-limit:

```json
{
  "google": ["key1", "key2"],
  "groq":   ["key1"],
  "nvidia": ["key1"]
}
```

---

## License

MIT. Do anything. Just don't be evil.
