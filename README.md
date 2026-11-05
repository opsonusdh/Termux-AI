# Termux-AI — Orion

A modular, self-correcting autonomous AI agent for Termux. Orion runs entirely on-device, uses free cloud LLM APIs for inference, and exposes a terminal chat loop with tool use, persistent memory, voice I/O, chunk-based context compression, and a full agentic execution layer.

---

## Features

- **Multi-provider LLM fallback** — Cycles through Google Gemini, OpenRouter, Groq, NVIDIA, and Ollama (cloud & local) models automatically. Rate-limited or invalid keys rotate to the next key for the same model; a transient server error (500/502/503/504) skips straight to the next model instead of retrying every key for it.
- **Tool use** — `run_code`, `read_file`, `write_file`, `browse_web`, `browser`, `save_memory`, `retrieve_memory`, `index_files`, `intermediate_print`, `sleep_mode`.
- **Enhanced Tool Suite** — 7 new utility tools added:
  - `search_in_files` - recursive file content search with advanced options and filters
  - `list_directory` - directory listing with configurable depth
  - `search_files` - filename pattern matching with wildcard support
  - `rename_file` - safe file/directory rename/move with protection
  - `delete_file` - secure file/directory deletion with safety checks
  - `http_request` - HTTP client for API calls with comprehensive headers
  - `get_datetime` - current time information in multiple formats
- **The app knows its own version** — a single `VERSION` constant in `core/__main__.py` is shown in the startup banner (`v3.3.1`), so the build you are talking to is never a guess.
- **Browser reads pages as Markdown** — `browse_web` and the persistent `browser` tool read the rendered DOM and hand back structure (headings, lists, tables, links), not a wall of text. One element can be pulled out on its own, and a screenshot is taken only when the model asks for one. See [Browser & Web](#browser--web).
- **Tool call transparency** — every tool call prints a collapsed one-line status by default; `Ctrl+O` (or `/expand`) reveals the full command/path and complete output. See [Tool Call Display](#tool-call-display).
- **Chunk-based context memory** — Conversation history is divided into stable numbered chunks. Old chunks are progressively compressed (short → micro → one-line summary) in a background thread. Raw chunks are permanently stored and retrievable by ID via `retrieve_chunk` / `list_chunks` tool calls.
- **Persistent memory** — Two-tier RAG system: personal facts (`memories.txt`) and indexed code/docs (`indexed_memory.txt`).
- **Agentic execution** — `/agent` triggers a Supervisor → Worker → Critic loop. Tasks are planned via `agent/planner.py`, executed with one retry, and persisted across restarts through `data/state.json`.
- **Enhanced Agent Interface** — Persistent agent mode state with direct prompts via `/agent [prompt]` syntax, clean toggle commands (`/normal`, `/chat`), `/expand`/`/collapse` for tool-detail visibility, and improved history tracking.
- **Modern Terminal Rendering** — Enhanced CLI code block styling with open-right gutter design, cleaner borders, and improved terminal integration.
- **Orchestration** — `orchestration/` provides multi-process task delegation (`Manager` → `Worker`) over a `multiprocessing.Queue` IPC channel.
- **Self-correction** — `reflection/` logs every execution outcome and automatically retries failures via `attempt_correction()`.
- **Voice I/O** — Optional STT via [Termux-STT](https://github.com/opsonusdh/Termux-STT) and TTS via `edge-tts` + `mpv`.
- **WhatsApp integration** — Send/receive messages and enable busy mode via [Termux-WP](https://github.com/opsonusdh/Termux-WP).
- **Safe execution** — `permissions.py` validates every shell command before dispatch.
- **Autonomous mode (opt-in)** — `/autonomous on` bypasses the permission layer entirely; off by default. See [Autonomous Mode](#autonomous-mode).
- **Advanced LLM Client** — Enhanced model slot organization with agent-specific slots, advanced error handling for API failures, reasoning budget exhaustion management, native reasoning metadata preservation, and tool execution timeout tracking.
- **Notifications (opt-in)** — When `notify: true` in `config/config.json`, a system notification is sent via `termux-notification` **whenever the AI stops and waits for you** — a permission request, a confirmation, or a question — and after an agent task completes. Each kind of prompt gets its own notification, so the shade says what is being asked. See [Notifications](#notifications).

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
| `pygments` | `pip install pygments` — syntax highlighting for code blocks, tool calls and the permission prompt; the renderer falls back to plain text without it |
| **Browser (optional)** | |
| Chromium | `pkg install chromium` — what `browse_web` and `browser` drive |
| **Prompt (optional)** | |
| `prompt_toolkit` | `pip install prompt_toolkit` — multi-line editing and persistent history; the runtime falls back to plain `input()` without it |
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

`config/api.keys` must be valid JSON — copy `config/api.keys.template` and fill it in:

```json
{
  "google":     ["YOUR_GEMINI_KEY_1", "YOUR_GEMINI_KEY_2"],
  "openrouter": ["YOUR_OPENROUTER_KEY"],
  "groq":       ["YOUR_GROQ_KEY"],
  "nvidia":     ["YOUR_NVIDIA_KEY"],
  "ollama":     ["YOUR_OLLAMA_KEY"],
  "ollama-local": ["No-Need"]
}
```

An **empty list means that provider is skipped** — no key, no warnings about it in the
rotation. `ollama-local` is a local server and needs no key at all; the `No-Need`
placeholder is only what tells the loader to select that route.

```bash
# 4. Run
python core
```

Free API keys: [Google AI Studio](https://aistudio.google.com/) • [Groq Console](https://console.groq.com/) • [NVIDIA NIM](https://build.nvidia.com/) • [OPENROUTER](https://openrouter.ai/) • [OLLAMA](https://ollama.com/dashboard/)

---

## Directory Structure

```
~/Termux-AI
.
├── CHANGELOG.md
├── CODE_OF_CONDUCT.md
├── CONTRIBUTING.md
├── LICENSE.md
├── PROJECT_STRUCTURE.md
├── README.md
├── SECURITY.md
├── agent
│   ├── __init__.py
│   ├── executor.py
│   ├── planner.py
│   ├── state_manager.py
│   └── validator.py
├── config
│   ├── api.keys
│   ├── api.keys.template
│   ├── capability_registry.json
│   ├── config.json
│   └── whatsapp_filters.json
├── core
│   ├── PROMPT.md
│   ├── __main__.py
│   ├── app_config.py
│   ├── context_compressor.py
│   ├── context_manager.py
│   ├── display_state.py
│   ├── google_api.py
│   ├── google_oauth.py
│   ├── html_markdown.py
│   ├── input_handler.py
│   ├── interface.py
│   ├── llm_client.py
│   ├── models.py
│   ├── notifier.py
│   ├── permissions.py
│   ├── plugin_manager.py
│   ├── plugin_runtime.py
│   ├── renderer.py
│   ├── runtime_log.py
│   ├── session_memory
│   │   ├── README.md
│   │   ├── integration.py
│   │   └── session_memory.py
│   ├── shell_parse.py
│   ├── shell_runner.py
│   ├── tools.py
│   ├── ui.py
│   └── whatsapp_manager.py
├── data
│   ├── cli_history
│   ├── context_session.json
│   ├── google_pending_auth.json
│   ├── google_tokens
│   ├── sessions
│   ├── state.json
│   └── validator_schema.json
├── indexed_memory.txt
├── instructions
│   ├── coding.md
│   ├── communication.md
│   ├── decision_making.md
│   ├── environment_and_tools.md
│   ├── memory_and_context.md
│   ├── orchestration_workflows.md
│   ├── readme.md
│   ├── reasoning.md
│   ├── release_and_maintenance.md
│   ├── security_and_privacy.md
│   ├── tool_efficiency.md
│   ├── troubleshooting.md
│   ├── verification.md
│   ├── whatsapp_and_notifications.md
│   └── skills
│       ├── INDEX.md
│       ├── core
│       └── imported
├── logs
│   ├── chunk_summaries.json
│   ├── chunks.jsonl
│   ├── context_archive.jsonl
│   ├── history.jsonl
│   ├── log.txt
│   ├── problem.txt
│   ├── reflection.jsonl
│   ├── todos.json
│   └── whatsapp_log.jsonl
├── memories.txt
├── orchestration
│   ├── __init__.py
│   ├── manager.py
│   ├── orchestrator.py
│   ├── protocol.py
│   └── worker.py
├── paths.py
├── reflection
│   ├── __init__.py
│   ├── reflector.py
│   └── self_correction.py
├── setup.sh
├── tests
│   ├── conftest.py
│   ├── fixtures
│   └── test_*.py
└── tools
    ├── __init__.py
    ├── tool_wrappers.py
    ├── wrapper_system_hostname.py
    ├── wrapper_system_uptime.py
    ├── wrapper_termux_audio.py
    ├── wrapper_termux_battery_status.py
    ├── wrapper_termux_brightness.py
    ├── wrapper_termux_camera.py
    ├── wrapper_termux_clipboard.py
    ├── wrapper_termux_contacts.py
    ├── wrapper_termux_filepicker.py
    ├── wrapper_termux_location.py
    ├── wrapper_termux_sensors.py
    ├── wrapper_termux_sms.py
    ├── wrapper_termux_telephony.py
    ├── wrapper_termux_torch.py
    ├── wrapper_termux_vibrate.py
    ├── wrapper_termux_volume.py
    ├── wrapper_termux_wallpaper.py
    └── wrapper_termux_wifi_scaninfo.py
```

---

## Configuration (config/config.json)

The following options are available in `config/config.json`:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `stt_path` | string | `~/Termux-AI/Termux-STT` | Path to Termux-STT module |
| `tts_enabled` | boolean | `false` | Start with voice mode enabled |
| `use_groq` | boolean | `false` | Use Groq TTS (faster, requires internet) |
| `show_details` | boolean | `false` | Show expanded tool output by default |
| `autonomous` | boolean | `false` | Bypass permission layer (opt-in) |
| `notify` | boolean | `true` | Push a notification when the AI waits for you (permission / confirmation / question) and when an agent task completes |

Example `config.json`:
```json
{
    "stt_path": "/data/data/com.termux/files/home/Termux-AI/Termux-STT",
    "tts_enabled": false,
    "use_groq": false,
    "show_details": false,
    "autonomous": false,
    "notify": true
}
```

---

## Agent Mode

```
YOU > /agent          # run one step: resolve next task → worker → critic
YOU > /agent auto     # loop until no pending tasks or a failure
YOU > /agent [prompt] # direct agent prompt with custom instructions
YOU > /normal         # switch to standard chat mode
YOU > /chat           # switch to conversational mode
```

Initialize a project and add tasks through normal chat — Orion uses the `initialize_project` and `add_subtask` tools. State persists in `data/state.json` and survives restarts.

### Notifications

When `notify: true` in `config/config.json`, a system notification is sent via `termux-notification`.

**When the AI waits for you** — the runtime blocks on `input()`, and on a phone
the terminal is usually in the background, so nothing on screen says that
something is being asked. Each of those moments pushes its own notification:

| The AI is… | Notification title | The body carries |
|---|---|---|
| asking for permission to run something | `Termux-AI: Permission needed` | every command in the batch and the reason for it |
| waiting for a yes/no confirmation | `Termux-AI: Confirmation needed` | the question and which key Enter answers |
| waiting for an answer to a question | `Termux-AI: Question` | the question and the options, if there are any |

The permission prompt, the `confirm` tool and the `ask_user` tool are the three
places that block, so those are the three places that notify.

**When an agent task finishes** — after `/agent auto` completes its loop,
`/agent [prompt]` finishes an inline task, or persistent agent mode completes
a task, the notification says "Termux-AI Agent — Agent task completed".

Switch it without touching the file:

```
YOU > /notify true      # on — and a test notification proves the plumbing
YOU > /notify false     # off — the AI stops pinging you while it waits
YOU > /notify toggle    # flip it (reads the current state from disk first)
YOU > /notify status    # what is it doing right now?
```

`/notify status` reads `config/config.json` at that moment, so it reports the
state the runtime is actually in — even if the file was edited by hand while
the session was running. An unrecognised argument (`/notify wat`) shows the
usage instead of being sent to the model as a chat message.

To disable it permanently: set `"notify": false` in `config/config.json`.
A missing Termux:API package is not an error — the notification is simply
skipped, and the runtime carries on.

---

## Autonomous Mode

```
YOU > /autonomous on       # bypass the permission layer entirely
YOU > /autonomous off      # restore normal permission gating (default)
YOU > /autonomous status   # check which mode is currently active
```

While autonomous mode is on, `run_code` never asks for approval — not for writes into `core/`, not for commands on the forbidden list, nothing. It's **off by default**, and the setting persists in `config/config.json`. Turn it on only when you trust what you're about to have the agent do: there's no per-action confirmation left to catch a mistake once it's on.

---

## Tool Call Display

Every tool call prints to the terminal as it runs. Collapsed (the default) gives a one-line status per call; `Ctrl+O` / `/expand` reveals the full command or path plus the complete output (see [Input Controls](#input-controls)).

The code a call shows is **syntax-highlighted**, with the same Pygments highlighter the markdown renderer uses for code blocks. A file's contents are highlighted with the lexer of *that file's* extension; a shell command — and the permission prompt — are always highlighted as **bash**. Expanded, the snippet is drawn in the same gutter box a fenced code block gets.

Running a command (`shell`) — collapsed:

```
[EXEC]
echo "step 1"
echo "step 2"
echo "step 3"
[TOOL DONE] (0.34s)
```

only the first three lines of the command are shown, and expanded shows the rest plus the full output:

```
[EXEC] | timeout=30
╭── bash ─────────────
│ echo "step 1"
│ echo "step 2"
│ echo "step 3"
╰────────────────────
[OUTPUT]
step 1
step 2
step 3
[TOOL DONE] (0.34s)
```

Editing a file (`write_file`) / reading one (`read_file`) — collapsed shows nothing but the bare tag:

```
[EDITING FILE]
[TOOL DONE] (0.08s)
```

expanded — the content that is about to be written is highlighted with the lexer of the **target file**:

```
[EDITING FILE] core/llm_client.py | mode: overwrite | lines: 10-25
content:
╭── python ─────────────────
│ def ask_ai(prompt):
│     return client.chat(...)
╰───────────────────────────
[OUTPUT]
Successfully wrote 42 characters to core/llm_client.py.
[TOOL DONE] (0.08s)
```

A read brings the file's own source back, highlighted with the lexer of **that file**. The line numbers stay outside the colouring, so the indentation of the source survives the highlighter:

```
[READING FILE] core/llm_client.py | lines: all
[OUTPUT]
╭── python ─────────────────
│ 10| def ask_ai(prompt):
│ 11|     return client.chat(...)
╰───────────────────────────
[TOOL DONE] (0.08s)
```

The permission prompt highlights the command it is asking about the same way:

```
[PERMISSION] The AI wants to run:
╭── bash ─────────────────
│ cd /tmp
╰────────────────────────
  Reason: working-directory change via 'cd' requires explicit permission
  Allow all listed actions? [y/n]
```

Every other tool follows the same shape — `[TOOL_NAME]` collapsed, every one of its arguments plus a full `[OUTPUT]` block expanded — and a failed call closes with `[TOOL ERROR] (Xs)` instead of `[TOOL DONE]`. This is presentation only: the model always receives the complete tool output regardless of what the terminal happens to be showing.

Expanded shows **all** of a call, not a summary:

- `write_file` shows the path, the mode, the affected line range **and the content being written**.
- `find_replace` shows the path and the **complete** find and replace text.
- `shell` shows the whole command and the timeout budget.
- Every other tool shows all of its arguments, one per line — including the tools that have no obvious "main" argument (`delegate_subtask`, `create_calendar_event`, `share_drive_file`, …), which used to show nothing at all.

### Delegation

A delegated sub-task shows the whole hand-over in expanded mode — the manager's command, the context it supplied, the tools the sub-AI may use, the iteration budget and the system prompt — followed by every tool call the sub-AI makes (with its arguments) and the output each call returned:

```
[DELEGATE COMMAND] intelligence=high iterations=5
[DELEGATE TASK]
    Audit the shell parser
[DELEGATE CONTEXT]
    core/shell_parse.py holds the parser
[DELEGATE TOOLS] read_file, search_in_files, shell
[DELEGATE TOOL] shell
    {
      "bash": "grep -n parse core/shell_parse.py"
    }
[DELEGATE RESULT] shell
    42:def parse(command):
```

---

## Browser & Web

Two tools read the web:

| Tool | What it does |
|---|---|
| `browse_web` | One-shot: open a URL, read it, close it. |
| `browser` | A **persistent session** — `open` returns a short id (`b1`), and every later call works on the same live page (scroll position, cookies, half-filled forms). |

The page comes back as **Markdown**, not a wall of text: headings stay headings, lists stay lists, tables stay tables, links keep their target, and the furniture nobody reads — nav bars, footers, cookie banners, hidden panels — is dropped before the conversion starts.

The `search` argument finds **one** element and returns it together with everything inside it. The query may be a class name (a class written with spaces, `weather data`, matches an element carrying all of those classes), an id, a tag name (`div`, `a`) or a full CSS selector (`#results .row`) — so a box of values arrives as a structure instead of three loose lines.

**The page is read when it is ready.** Both tools wait for the DOM to settle (1.5 s of quiet, hard cap 10 s) instead of trusting the load event — a search engine reports "loaded" while the results are still arriving.

**Screenshots are opt-in.** `screenshot=true` (default false) captures the viewport. An unasked-for screenshot is never taken, never attached to the reply, and can never move the turn to a different model.

**Never guess where a button is.** The `find` action lists every visible button, link and form field with the label a human would read and a ready-to-use CSS selector. A click is then aimed by `text`, `index` or `selector`, and the position is resolved against the live DOM at the moment of the click.

Requires Chromium (`pkg install chromium`). Without it the tools fall back to a no-JS scraper that answers in the same Markdown shape.

The Chromium profile and the screenshots live under `workspace/` (`.browser/`, `screenshots/`) — `/tmp` is not writable on Termux. Only the 60 most recent screenshots are kept.

---

## Session Log

Everything the runtime does is appended to `logs/log.txt` **as it happens** — the user's message, the model's reasoning, every tool call with its full arguments, every tool result with its full output, the delegation traffic and the final replies.

Each write is flushed immediately, so a tool call is in the log *while it is still running*, not after the turn ends:

```
$ tail -f logs/log.txt
[2026-11-03 16:57:19] [session]
    session started (pid 24460) — live log: .../logs/log.txt
[2026-11-03 16:57:19] [user] input | chat
    Use the get_datetime tool to tell me the current date and time
[2026-11-03 16:57:26] [reasoning] ollama/beam:cloud
    I need to call the get_datetime tool to find out the current date and time.
[2026-11-03 16:57:26] [tool:call] get_datetime
[2026-11-03 16:57:26] [tool:result] get_datetime (0.00s)
    Local Time : 2026-11-03 16:57:26
    Day of Week: Tuesday
[2026-11-03 16:57:28] [reply] final
    It's Tuesday, November 03, 2026 at 4:57 PM IST.
```

If the session is cut short mid-turn, the log still shows exactly what was being done — the last tool call is on disk with its arguments, and the result is simply missing.

The file is capped at **1,000,000 lines**. When it grows past that, the oldest lines are dropped, oldest first, so the log keeps the most recent history instead of eating the device's storage. The trim streams the file through a temporary copy and swaps it in atomically, so a reader tailing the log never sees a half-written file.

---

## Input Controls

| Key | Action |
|---|---|
| `Enter` | Send the message |
| `Ctrl+N` | Insert a newline, for multiline messages |
| `Ctrl+J` | Also sends (compatibility alias) |
| `Ctrl+O` | Toggle collapsed ↔ expanded tool/reasoning detail |

If a terminal doesn't pass a key combo through, the same toggle is available as a command, in both `/agent` and normal chat:

```
YOU > /expand      # aliases: /e, /details on, /view extended
YOU > /collapse    # aliases: /c, /details off, /view collapsed
```

---

## Sessions

```
YOU > /session list         # saved sessions, rendered as a readable list
YOU > /session save         # save the current conversation
YOU > /session resume <id>  # continue a saved session
YOU > /session new          # close the current conversation, keep everything else
YOU > /session delete <id>  # remove a saved session
```

`/session new` is the `/clear` of Termux-AI: the chat history, the agent history and the chunk context the model is fed go away, and the next message starts from a blank slate. It deliberately does **not** re-run the startup UI (no banner, no status row) and does not touch the mode (`/agent` stays in agent mode), the preferences, the display state or the plugins.

The wipe is durable: the chunk store is not deleted, a **context boundary** is moved forward and persisted (`data/context_session.json`), so the closed conversation stops being injected into every request and does not come back after a restart — while the chunks themselves stay retrievable with `retrieve_chunk`, so the model can still dig out earlier work when you ask about it. The session draft is archived as a normal saved session rather than dropped.

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

When the assembled context no longer fits the model's window, the oldest passes are
compressed into a digest and the **raw text is archived on disk**
(`logs/context_archive.jsonl`). A marker in the conversation names the archive id, so
the model can pull the original back with `retrieve_archive` and `list_archives` — the
digest is a summary, the archive is the evidence.

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

## Plugins

Plugins add whole capability groups, and each one can be switched on or off
independently. Two kinds exist:

- **Local plugins** — `whatsapp` pairs on the device, so it needs no account
  handshake. Enabling it also **starts what it needs to run**: the Termux-WP
  node gateway (`bot.js`) is launched for you, so the WhatsApp tools work
  right away instead of failing with "connection refused". A gateway that
  dies is restarted automatically, and an orphaned browser holding the
  WhatsApp profile is cleared before the next start.
- **Account plugins** — `gmail`, `google_calendar`, `google_drive`. Enabling
  one is only *half* the job: the account still has to grant access before the
  tools become usable.

```bash
/plugin                      # show every plugin and its state
/plugin status gmail         # one plugin in detail
/plugin status whatsapp      # …including whether this device is paired
/plugin setup gmail          # connect the account (shows the URL to approve)
/plugin setup whatsapp       # pair this device (shows the QR — once)
/plugin enable gmail         # enable — and start the permission request
/plugin enable whatsapp      # enable — and start the Termux-WP gateway
/plugin disable gmail        # stop offering its tools
/plugin disconnect gmail     # revoke the stored Google access
```

The plugin manager itself (`manage_plugin`) is always in the assistant's tool
list, and a plugin's tools join that list the moment it is enabled — no
restart, no repeat of the request.

#### Pairing WhatsApp

The WhatsApp session is paired by scanning a QR code, and that code is a live
secret — whoever scans it gets read/write access to your account. So the
plugin never prints it on its own: it is captured, and drawn **only** when you
ask for it.

```bash
/plugin setup whatsapp       # the pairing code appears on your screen, once
```

If the device is **already paired** and you ask for the code again, there is no
code to give — a re-pair is what you are after — so the plugin stops the
gateway and switches itself off instead. Run `/plugin enable whatsapp` to bring
it back.

An unpaired gateway is silent: it does not reprint the code while you are
chatting, and it does not announce that it connected or print your number.
`/plugin status whatsapp` tells you the pairing state when you ask for it.

### Connecting a Google account

You can also just ask: *"turn on Gmail"* — the assistant calls `manage_plugin`
and hands you the link.

```bash
/plugin setup gmail
```

That prints a URL. Open it **in the browser on this device**, sign in with the
account you want to connect, and approve the permissions:

```
┌─ Gmail setup — Google OAuth ─────────────┐
  Open this URL in the browser on this device —
  the approval is sent straight back to Termux-AI:
  https://accounts.google.com/o/oauth2/v2/auth?...
  Sign in and approve the requested permissions.
  (waiting for the approval… press Ctrl+C to abort)
```

Termux-AI runs a throwaway local web server and Google sends the approval
straight back to it, so nothing has to be copied. If you approve on another
device instead, the redirect cannot reach this one — copy the address-bar URL
(it contains `?code=…`) and paste it back; that works too.

Once you approve, the token is stored and the Gmail tools become visible to
the model.

**You need a Google OAuth client first (about 2 minutes, one time):**

1. **OAuth consent screen** — <https://console.cloud.google.com/auth/overview>.
   Choose **External**, fill in the app name and a support email, and add your
   own Google account as a **test user** (test users can approve sensitive
   scopes without waiting for a review).
2. **Enable the API** in the API library:
   - Gmail — <https://console.cloud.google.com/apis/library/gmail.googleapis.com>
   - Drive — <https://console.cloud.google.com/apis/library/drive.googleapis.com>
   - Calendar — <https://console.cloud.google.com/apis/library/calendar-json.googleapis.com>
3. **Create the client** — <https://console.cloud.google.com/auth/clients> →
   *Create credentials → OAuth client ID* → application type **Desktop app**.
   > **Do not pick "TVs and Limited Input devices".** That client type only
   > supports the device-code flow, which cannot request the Gmail, Calendar or
   > Drive scopes, and Google blocks the local redirect for it — you get
   > *"Access blocked: … request is invalid"*. A **Desktop app** client is the
   > one that supports the local redirect Termux-AI uses.
4. **Copy the Client ID and Client secret** from the same page
   (<https://console.cloud.google.com/auth/clients>). The next
   `/plugin setup gmail` asks for them, or you can export them instead so they
   never touch the repo:

```bash
export GOOGLE_CLIENT_ID="..."
export GOOGLE_CLIENT_SECRET="..."
```

Reference: [Desktop app flow](https://developers.google.com/identity/protocols/oauth2/native-app) ·
[device flow for TVs](https://developers.google.com/identity/protocols/oauth2/limited-input-device) ·
[all scopes](https://developers.google.com/identity/protocols/oauth2/scopes) ·
[why the local redirect works for Desktop clients only](https://developers.google.com/identity/protocols/oauth2/resources/loopback-migration)

### Where the credentials live

| File | Contents | Permissions |
|---|---|---|
| `config/google_oauth.json` | OAuth client id + secret | `0600`, permission-gated |
| `data/google_tokens/<service>.json` | access + refresh tokens | `0600`, permission-gated |
| `data/google_pending_auth.json` | in-flight PKCE verifier | `0600`, permission-gated |

All three are treated like `config/api.keys`: they are protected in every
permission mode, including autonomous, and are git-ignored. Tokens are never
printed — `/plugin status` reports the connected *account*, not the token.

If a plugin is enabled but the account is not connected, the tools stay hidden
from the model and a call returns an explicit message naming the command to
run — instead of failing in a way that looks like a bug.

---

## api.keys Format

Multiple keys per provider rotate round-robin and are retired on an invalid credential or a rate-limit; a transient server error (5xx) skips the rest of that model's keys entirely and moves to the next model instead:

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

A provider with an **empty list is skipped** by the rotation. `ollama-local` talks to a
local server and needs no key — the `No-Need` placeholder is only what tells the loader
to select that route.

---

## License

MIT. Do anything. Just don't be evil.
