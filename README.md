# Terminal Autonomous AI Agent: Orion (Termux)

A terminal-native autonomous AI agent for Termux powered by Gemini through the OpenAI-compatible API interface.

The assistant identity is **Orion**.

This project is not a regex-driven shell parser anymore.
It uses native tool calling inside the runtime, with internal orchestration for:
- command execution
- memory retrieval
- filesystem-safe actions
- indexed retrieval
- voice wake handling

The agent is designed to behave like a practical terminal operator that can reason, inspect, remember, and act inside a constrained Termux environment.

This project is not trying to fake general intelligence.

It is focused on controlled autonomy inside a local terminal runtime. A rare concept these days. Most "AI agents" currently resemble a caffeinated while-loop wearing sunglasses.

---

# What Orion can do

- reason over tasks step by step
- call tools internally
- execute shell commands through a validated runtime tool
- inspect files and directories
- maintain persistent structured memory
- retrieve relevant memories automatically
- index large codebases and documents
- perform lightweight RAG retrieval
- render markdown cleanly in the terminal
- interact with supported Termux APIs
- scrape and structure webpages
- support wake-word voice activation
- operate in passive sleep mode
- work inside a sandboxed filesystem model

---

# Important Architecture Note

The agent does **not** rely on old-style shell block extraction.

Older approaches based on:
- markdown command fences
- regex scraping
- bash extraction from model text

have been replaced by:

- native tool calling
- internal tool dispatch
- validated execution
- direct tool feedback loops
- structured runtime orchestration

The runtime itself manages execution and tool results.

The model does not emit fake terminal scripts for the outer runtime to scrape.

That architecture gets unstable very quickly once the model starts improvising. Which it absolutely will. Machines inherit humanity's bad habits faster than expected.

---

# Core Runtime Tools

The assistant currently supports:

- `run_code`
- `save_memory`
- `retrieve_memory`
- `read_file`
- `write_file`
- `index_files`
- `web_scrape`
- `sleep_mode`

These are runtime-managed tools, not prompt-roleplay abstractions.

---

# Sandboxed Filesystem

The AI operates primarily around:

```text
~/Termux-AI
```

---

## Permission Model

| Action | Allowed |
|---|---|
| Read files anywhere | ✅ |
| Write/delete inside `~/Termux-AI/workspace` | ✅ |
| Modify `~/Termux-AI/core` | ❌ Requires permission |
| Modify outside `~/Termux-AI` | ❌ Requires permission |
| Dangerous system commands | ❌ Requires permission |

The permission system is:
- path-aware
- workspace-aware
- context-sensitive

instead of relying purely on naive keyword filtering.

Safe autonomous actions inside the workspace are allowed automatically.

Examples:
- downloading files
- deleting temporary files
- generating projects
- media processing
- local experimentation

Protected areas and risky operations still require confirmation.

---

# Persistent Memory

The agent stores durable personal memory in:

```text
~/Termux-AI/memories.txt
```

Memory supports:
- structured entries
- priorities
- tags
- semantic retrieval
- automatic prompt injection
- tool-based saving and retrieval

The memory system is intended for stable knowledge such as:
- user preferences
- workflow habits
- project details
- instructions
- recurring environment facts

---

# Indexed Knowledge Memory

Large codebases and documents are stored separately in:

```text
~/Termux-AI/indexed_memory.txt
```

This system supports:
- chunked indexing
- semantic retrieval
- lightweight RAG injection
- codebase awareness
- document ingestion

Unlike personal memory, indexed memory is:
- bulk-oriented
- automatically chunked
- relevance-filtered
- capped during retrieval to avoid context flooding

The runtime uses:
- `index_files()` for ingestion
- semantic scoring during retrieval
- automatic memory injection into prompts
- separate retrieval thresholds for indexed content

This allows Orion to reason over projects without dumping entire repositories into context like a deranged photocopier.

---

# Semantic Memory Retrieval

The agent does not blindly inject entire memory files into prompts.

Retrieval uses:
- keyword scoring
- category matching
- priority weighting
- heap-ranked retrieval
- separate indexed-memory thresholds

This keeps retrieval focused and reduces context pollution.

Priority-10 instructions always surface automatically.

---

# Markdown Terminal Renderer

The response renderer formats terminal output using ANSI styling.

Supported formatting includes:
- headings
- bold text
- italic text
- inline code
- fenced code blocks
- bullets
- numbered lists
- dividers
- readable terminal colors

This keeps terminal output readable without raw markdown clutter leaking into the shell.

Human civilization invented ANSI escape codes and somehow still uses Electron for calculators. Reality remains difficult to process.

---

# Voice Wake System

When paired with the companion project:

0

Orion supports:
- live speech transcription
- passive sleep mode
- wake-word detection
- AI-based wake relevance classification
- optional TTS responses using `edge-tts`

---

## Sleep Mode Behavior

When instructed to sleep:
- the reasoning loop pauses
- Whisper speech detection remains active
- Orion listens for the wake word
- detected speech is checked for relevance using a lightweight AI classifier
- irrelevant chatter is ignored
- relevant requests wake the assistant automatically

This prevents accidental activation from unrelated speech while keeping the assistant responsive.

---

# Termux API Integration

The runtime can integrate with supported local utilities such as:
- `termux-media-player`
- `termux-notification`
- `termux-tts-speak`
- other installed Termux utilities

This is handled through runtime tools, not fake text instructions.

---

# Project Structure

```text
~/Termux-AI $ tree
.
├── CONTRIBUTING.md
├── LICENSE
├── README.md
├── api.keys
├── config.json
├── core
│   ├── __main__.py
│   ├── interface.py
│   ├── llm_client.py
│   ├── permissions.py
│   ├── prompt.py
│   ├── renderer.py
│   └── tools.py
├── indexed_memory.txt
├── log.txt
├── memories.txt
├── Termux-STT
├── workspace
└── setup.sh

```

---

# Components

## `interface.py`

Main terminal interface.

Responsibilities:
- user interaction
- conversation loop
- voice handling
- displaying output
- passing prompts into the runtime

---

## `llm_client.py`

Core inference engine.

Handles:
- Gemini API access through the OpenAI-compatible endpoint
- tool calling
- retries
- overload recovery
- API key rotation
- model fallback handling
- automatic memory injection

---

## `tools.py`

Runtime tool subsystem.

Handles:
- shell execution
- memory operations
- indexing
- file editing
- web scraping
- wake mode
- TTS
- RAG support

This effectively became the operational backbone of the runtime. The inevitable fate of all "utility files."

---

## `permissions.py`

Sandbox enforcement layer.

Responsibilities:
- path checks
- workspace protection
- permission prompts for risky operations
- command validation

---

## `renderer.py`

ANSI markdown renderer.

Responsibilities:
- formatting responses
- code block display
- list formatting
- inline markdown styling
- terminal readability

---

## `prompt.py`

Behavior tuning layer.

Contains:
- the system prompt
- runtime behavior rules
- operational guidance
- autonomy constraints

---

# Setup

## Install Dependencies

```bash
pkg install git
```

---

## Clone The Repository

```bash
git clone https://github.com/opsonusdh/Termux-AI
cd Termux-AI
bash setup.sh
```

---

## Optional Voice Support

Install the companion speech project:

```bash
pkg install mpv
pip install edge-tts

git clone https://github.com/opsonusdh/Termux-STT/
cd Termux-STT
bash setup.sh
cd ..
```

---

# Create API Keys File

Create:

```text
~/Termux-AI/api.keys
```

Example:

```text
AIza...
AIza...
```

One API key per line.

The runtime automatically rotates keys when:
- rate limits occur
- overloads happen
- provider-side failures appear

---

# Running

From the project root:

```bash
cd ~/Termux-AI
python core
```

Or run the module directly depending on how `__main__.py` is configured.

---

# Example Session

## User

```text
YOU > create a small cli project
```

## Orion

The assistant:
- inspects context
- retrieves relevant memory
- plans the task
- calls tools internally
- creates files safely
- validates outputs
- returns structured results

---

# Security Model

## Protected Core

```text
~/Termux-AI/core
```

Contains protected runtime logic and core orchestration.

Modification requires explicit permission.

---

## Workspace

```text
~/Termux-AI/workspace
```

Safe writable zone for:
- generated files
- downloads
- experiments
- temporary project artifacts

The AI may:
- create files
- download content
- delete temporary artifacts
- generate projects
- run experiments

without requiring repeated confirmation.

This allows Orion to:
- use `yt-dlp`
- process media with `ffmpeg`
- generate scripts
- create build artifacts
- run local automation workflows

without interrupting the user every thirty seconds like a nervous intern.

---

## External System

The broader system may be inspected, but risky actions require confirmation.

Examples:
- modifying system directories
- killing unrelated processes
- altering protected runtime files
- privileged API access

---

# Current Capabilities

- autonomous shell execution
- semantic memory retrieval
- indexed RAG retrieval
- long-term memory persistence
- markdown terminal rendering
- native tool calling
- multi-step reasoning
- filesystem management
- wake-word voice activation
- passive sleep mode
- AI-based wake relevance classification
- web scraping
- structured file editing
- media playback
- Termux API integration
- retry-based recovery
- structured permissions
- API key rotation
- model fallback handling

---

# Current Limitations

- no streaming tool execution yet
- no async task scheduler
- retrieval is keyword-weighted rather than embedding-based
- very large contexts still require trimming
- no rollback or snapshot system
- no persistent background agents yet

---

# Future Ideas

- vector memory
- streaming responses
- async execution
- background tasks
- plugin system
- execution rollback
- command confidence scoring
- multi-agent coordination
- local embeddings
- persistent wake daemon

---

# Philosophy

This project is intended to be:
- useful
- inspectable
- constrained
- recoverable
- extensible

The architecture intentionally favors:
- explicit runtime control
- inspectable orchestration
- constrained autonomy
- local-first execution
- recoverable behavior

over opaque cloud-agent abstractions.

The goal is controlled autonomy, not chaotic shell possession.

---

# License

MIT License

Use responsibly.

---

# Final Note

- This project began before OpenClaw existed.
- It later evolved into a native tool-calling runtime.
- It is designed to integrate with other Termux ecosystem projects.
- Improvements, testing, and architectural suggestions are welcome.

The project is still evolving, but the core direction is stable:
> build a practical local autonomous terminal agent that remains understandable by humans.

