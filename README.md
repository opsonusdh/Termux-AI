# Termux-AI — Orion

A modular, self-correcting autonomous AI agent for Termux. Orion runs entirely on-device, uses free cloud LLM APIs for inference, and exposes a terminal chat loop with tool use, persistent memory, voice I/O, and an agentic orchestration layer.

---

## Features

- **Multi-provider LLM fallback** — Cycles through Google Gemini, Groq, and NVIDIA models automatically; rotates API keys on rate-limits.
- **Tool use** — `run_code`, `read_file`, `write_file`, `web_scrape`, `save_memory`, `retrieve_memory`, `index_files`, `sleep_mode`.
- **Persistent memory** — Two-tier RAG system: personal facts (`memories.txt`) and indexed code/docs (`indexed_memory.txt`).
- **Voice I/O** — Optional STT via [Termux-STT](https://github.com/opsonusdh/Termux-STT) and TTS via `edge-tts` + `mpv`.
- **Agentic orchestration** — `planner.py` → `executor.py` → `validator.py` pipeline with multi-process delegation via `orchestration/`.
- **Self-correction** — `reflection/` module logs execution outcomes and retries failures automatically.
- **Safe execution** — `permissions.py` validates every shell command before dispatch.
- **Whatsapp integration** — `whatsapp_manager.py`
---

## Requirements

| Dependency | Install |
|---|---|
| Python 3.10+ | `pkg install python` |
| Rust / cmake / clang | `pkg install rust cmake clang which` |
| `openai` SDK | `pip install openai` |
| `beautifulsoup4` | `pip install beautifulsoup4` |
| `requests` | `pip install requests` |
| **Voice (optional)** | |
| `edge-tts` | `pip install edge-tts` |
| `mpv` | `pkg install mpv` |
| Termux-STT | See [Termux-STT repo](https://github.com/opsonusdh/Termux-STT) |
| Termux-WP (WhatsApp support) | See [Termux-WP repo](https://github.com/opsonusdh/Termux-WP) |


---

## Setup

```bash
# 1. Clone
git clone https://github.com/opsonusdh/Termux-AI ~/Termux-AI
cd ~/Termux-AI

# 2. Install dependencies
bash setup.sh
pip install openai beautifulsoup4 requests

# 3. Add API keys
# Create api.keys in JSON format:
cat > ~/Termux-AI/api.keys << 'EOF'
{
  "google": ["YOUR_GEMINI_KEY_1", "YOUR_GEMINI_KEY_2"],
  "groq":   ["YOUR_GROQ_KEY"],
  "nvidia": ["YOUR_NVIDIA_KEY"]
}
EOF

# 4. Run
cd ~/Termux-AI/
python3 core
```

Free API keys: [Google AI Studio](https://aistudio.google.com/) · [Groq Console](https://console.groq.com/) · [NVIDIA NIM](https://build.nvidia.com/)

---

## Directory Structure

```
~/Termux-AI/
├── core/
│   ├── __main__.py          # Entry point — run this
│   ├── interface.py         # Chat loop, voice toggle, STT integration
│   ├── llm_client.py        # Multi-provider LLM client with fallback
│   ├── tools.py             # All tool implementations (run_code, memory, etc.)
│   ├── prompt.py            # System prompt
│   ├── renderer.py          # Terminal markdown renderer
│   └── permissions.py       # Command safety validator
│   └── whatsapp_manager.py       # whatsapp integration 
├── orchestration/
│   ├── orchestrator.py      # High-level task delegation
│   ├── manager.py           # Multi-process task manager
│   ├── worker.py            # Worker process (shell / python / mock)
│   └── protocol.py          # IPC via multiprocessing.Queue
├── reflection/
│   ├── __init__.py          # ReflectionLoop — logs & replays failures
│   ├── reflector.py         # Failure analyser
│   └── self_correction.py   # Auto-retry on validation failure
├── tools/
│   ├── tool_wrappers.py     # notify, toast, dialog, tts_speak
│   ├── wrapper_termux_battery_status.py
│   └── wrapper_termux_wifi_scaninfo.py
├── instructions/            # Agent-facing operational manuals
│   ├── coding.md
│   ├── reasoning.md
│   ├── orchestration_workflows.md
│   └── environment_and_tools.md
├── data/
│   ├── state.json           # Persistent agent state
│   ├── capability_registry.json
│   └── validator_schema.json
├── paths.py                 # Centralised path constants
├── planner.py               # Task planning
├── executor.py              # Task execution
├── validator.py             # Execution validation
├── state_manager.py         # State read/write
├── context_manager.py       # Conversation context
├── api.keys                 # API keys (JSON, gitignored)
├── config.json              # STT path, TTS toggle (gitignored)
├── setup.sh                 # Dependency installer
└── workspace/               # Scratch space for development tasks (gitignored)
```

---

## Voice Mode

Voice requires [Termux-STT](https://github.com/opsonusdh/Termux-STT), `edge-tts`, and `mpv` installed.

```
# In the chat loop:
YOU > start voice      # switch to voice input + TTS output
YOU > stop voice       # switch back to keyboard
```

Set `"tts_enabled": true` in `config.json` to start in voice mode by default.

---
## Whatsapp integration 

Requires [Termux-WP](https://github.com/opsonusdh/Termux-STT), `node` and various node modules.

```
# In the chat loop
YOU > Hey Orion, I will be busy for an hour or so. Can you enable busy mode in whatsapp?

---
## api.keys Format

`api.keys` uses JSON. Multiple keys per provider are rotated round-robin and retired on rate-limit:

```json
{
  "google": ["key1", "key2", ...],
  "groq":   ["key1", "key2", ...],
  "nvidia": ["key1", "key2", ...]
}
```

---

## License

MIT licence. 
Do any thing. Just don't be evil.
