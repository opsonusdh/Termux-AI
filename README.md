# Termux-AI — Orion

A modular, self-correcting autonomous AI agent for Termux. Orion runs entirely on-device, uses free cloud LLM APIs for inference, and exposes a terminal chat loop with tool use, persistent memory, voice I/O and a full agentic execution layer.

---

## Index

1. [Requirements](#requirements)
2. [Setup](#setup)
3. [Usage](#usage)
   - [Chat & agent mode](#chat--agent-mode)
   - [Commands](#commands)
   - [Voice mode](#voice-mode)
   - [WhatsApp](#whatsapp)
   - [Plugins](#plugins)
   - [Notifications](#notifications)
   - [Configuration](#configuration)
   - [API keys](#api-keys)
4. [Features at a glance](#features-at-a-glance)
5. [Documentation & history](#documentation--history)
6. [License](#license)

---

## Requirements

| Dependency | Install |
|---|---|
| Python 3.10+ | `pkg install python` |
| Rust / cmake / clang | `pkg install rust cmake clang which` |
| `openai` SDK | `pip install openai` |
| `httpx` | `pip install httpx` |
| `beautifulsoup4` | `pip install beautifulsoup4` |
| `requests` | `pip install requests` |
| `jsonschema` | `pip install jsonschema` |
| `pygments` | `pip install pygments` |
| Node.js (WhatsApp) | `pkg install nodejs` |
| Chromium (browser tools) | `pkg install chromium` |
| `prompt_toolkit` (optional) | `pip install prompt_toolkit` |
| `edge-tts` + `mpv` (voice, optional) | `pip install edge-tts` · `pkg install mpv` |
| [Termux-STT](https://github.com/opsonusdh/Termux-STT) (voice, optional) | see repo |
| [Termux-WP](https://github.com/opsonusdh/Termux-WP) (WhatsApp, optional) | see repo |

---

## Setup

```bash
git clone https://github.com/opsonusdh/Termux-AI ~/Termux-AI
cd ~/Termux-AI
bash setup.sh
nano config/api.keys   # copy config/api.keys.template and fill it in
python core
```

Free API keys: [Google AI Studio](https://aistudio.google.com/) • [Groq Console](https://console.groq.com/) • [NVIDIA NIM](https://build.nvidia.com/) • [OpenRouter](https://openrouter.ai/) • [Ollama](https://ollama.com/dashboard/)

---

## Usage

### Chat & agent mode

`python core` starts the chat loop. `/agent` switches to agent mode:

```
YOU > /agent          # run one step: resolve next task → worker → critic
YOU > /agent auto     # loop until no pending tasks or a failure
YOU > /agent [prompt] # direct agent prompt with custom instructions
YOU > /normal         # back to chat mode
```

Tasks are initialized in normal chat (`initialize_project`, `add_subtask`) and persist across restarts.

### Commands

| Command | Action |
|---|---|
| `/session list · save · resume <id> · new · delete <id>` | manage saved conversations; `new` starts a clean context |
| `/notify true · false · toggle · status` | push a notification when the AI waits for you |
| `/autonomous on · off · semi · status` | control the permission gate |
| `/expand` / `/collapse` (or `Ctrl+O`) | toggle full tool/reasoning detail |
| `/plugin list · status · enable · disable · setup · disconnect` | manage plugins |
| `/mcp list · status · tools · add · remove · enable · disable · refresh` | manage MCP servers |
| `start voice` / `stop voice` | toggle voice input + TTS output |

Keys: `Enter` sends · `Ctrl+N` inserts a newline · `Ctrl+O` toggles expanded tool detail.

### Voice mode

Requires [Termux-STT](https://github.com/opsonusdh/Termux-STT), `edge-tts` and `mpv`:

```bash
cd ~/Termux-AI
git clone https://github.com/opsonusdh/Termux-STT
cd Termux-STT && bash setup.sh
```

Then `start voice`, or set `"tts_enabled": true` in `config/config.json`.

### WhatsApp

Requires [Termux-WP](https://github.com/opsonusdh/Termux-WP) and Node.js:

```bash
cd ~/Termux-AI
git clone https://github.com/opsonusdh/Termux-WP
cd Termux-WP && bash setup.sh
```

Then: `/plugin setup whatsapp` (pairs this device), `/plugin enable whatsapp`. After that, plain requests work — *"Enable busy mode on WhatsApp for the next hour."*

### Plugins

`/plugin list` shows every plugin. `whatsapp` pairs on the device; the Google plugins (`gmail`, `google_calendar`, `google_drive`) need a one-time OAuth client — `/plugin setup gmail` prints the full walkthrough and the approval link.

### Notifications

```
YOU > /notify true      # on — and a test notification proves the plumbing
YOU > /notify false     # off
```

With `"notify": true` (the default), a system notification is pushed whenever the AI stops to wait — a permission request, a confirmation, a question — and when an agent task completes.

### Configuration

`config/config.json`:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `stt_path` | string | `~/Termux-AI/Termux-STT` | Path to Termux-STT |
| `tts_enabled` | boolean | `false` | Start in voice mode |
| `use_groq` | boolean | `false` | Use Groq TTS |
| `show_details` | boolean | `false` | Expanded tool output by default |
| `autonomous` | `false` \| `"semi"` \| `true` | `false` | Permission-gate level (`false` = ask, `"semi"` = ask on secrets only, `true` = full) |
| `notify` | boolean | `true` | Notify when the AI waits for you |
| `spill_trigger_tokens` | integer | `4000` | Tool results above this go to `data/spill/` |
| `spill_enabled` | boolean | `true` | Set `false` to keep every result inline |
| `mcp_servers` | list | `[]` | MCP servers to connect (stdio or HTTP) |

### API keys

`config/api.keys` — multiple keys per provider rotate round-robin; an empty list skips the provider. `ollama-local` needs no key.

```json
{
  "google":     ["key1", "key2"],
  "openrouter": ["key1"],
  "groq":       ["key1"],
  "nvidia":     ["key1"],
  "ollama":     ["your-ollama-key"],
  "ollama-local": ["No-Need"]
}
```

---

## Features at a glance

- **Multi-provider fallback** — Google Gemini, OpenRouter, Groq, NVIDIA, Ollama; keys rotate on rate limits, transient server errors skip to the next model.
- **Tool use** — shell with per-stage timeouts, file editing, web browsing (Chromium or fallback), image generation, device sensors, camera, WhatsApp, Google plugins, MCP servers.
- **Permission gate** — every shell command is checked; confidential files (keys, tokens, credentials) are gated in every mode.
- **Decision model chain** — `make_decision` and the wake-word check use free decision models instead of guesses; yes/no answers come back as probabilities.
- **Context memory** — chunked, progressively compressed history; raw text is archived and retrievable by id.
- **Persistent memory** — two-tier RAG (`memories.txt` + indexed code/docs).
- **Agent mode** — Supervisor → Worker → Critic loop with delegation by intelligence level.
- **Voice I/O** — STT via Termux-STT, TTS via `edge-tts` + `mpv`.
- **Skills** — fifteen core operational manuals loaded on demand via `skill(name)`.

Detailed feature and design notes live in [CHANGELOG.md](CHANGELOG.md#appendix--guides-moved-from-the-readme); the module map is in [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md).

---

## Documentation & history

| Document | Contents |
|---|---|
| [CHANGELOG.md](CHANGELOG.md) | Release history + the detailed guides moved out of this README |
| [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md) | Directory and module map |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute and run the tests |
| [SECURITY.md](SECURITY.md) | Security policy |

---

## License

MIT — see [LICENSE.md](LICENSE.md).
