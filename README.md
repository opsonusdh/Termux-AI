# Terminal Autonomous AI Agent (Termux)

A terminal-native autonomous AI agent for Termux powered by Gemini through the OpenAI-compatible API interface.

This project is **not** a regex-driven shell parser anymore.  
It uses **native tool calling** inside the runtime, with internal orchestration for command execution, memory retrieval, and filesystem-safe actions.

The agent is designed to behave like a practical terminal assistant that can reason, inspect, remember, and act inside a constrained Termux environment.

---

## What the agent can do

- reason over tasks step by step
- call tools internally
- execute shell commands through a validated runtime tool
- inspect files and directories
- maintain persistent structured memory
- retrieve relevant memories when needed
- render markdown cleanly in the terminal
- interact with supported Termux APIs
- work inside a sandboxed filesystem model

---

## Important architecture note

The agent **does not** rely on old-style `bash-run` blocks or command extraction from model text.

That older design has been replaced by:

- native tool calling
- internal tool dispatch
- validated command execution
- direct result feedback into the model loop

So the model does **not** output shell blocks for the runtime to scrape.  
The runtime calls tools directly and manages the loop internally.

---

## Features

### Native Tool Calling

The assistant uses tools such as:

- `run_code`
- `save_memory`
- `retrieve_memory`

These are handled by the runtime, not by parsing markdown command fences.

---

## Sandboxed Filesystem

The AI operates around:

```text
~/Termux-AI
```
### Permission model

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

instead of relying purely on naive keyword matching.

Safe autonomous actions inside the workspace are allowed automatically.

Examples:
- downloading files
- deleting temporary files
- generating projects
- media processing
- local experimentation

Protected areas and risky operations still require confirmation.

---

## Persistent Memory

The agent stores durable memory in:

```text
~/Termux-AI/memories.txt
```
Memory supports:
- structured entries
- priorities
- tags
- semantic retrieval
- automatic injection into prompts
- tool-based saving and retrieval

The memory system is intended for stable knowledge such as:
- user preferences
- workflow habits
- project details
- instructions
- recurring environment facts

---

## Semantic Memory Retrieval

The agent does not blindly dump the whole memory file into context.

Instead, it retrieves relevant memories using:
- keyword scoring
- category matching
- priority weighting
- heap-based ranking

This keeps memory retrieval focused and useful.

---

## Markdown Terminal Renderer

The response renderer formats terminal output with ANSI styling.

It supports:
- headings
- bold text
- italic text
- inline code
- fenced code blocks
- bullets
- numbered lists
- dividers
- readable color styling

This keeps assistant output usable in Termux without raw markdown clutter leaking through.

---

## Termux API Integration

The agent can use supported local tools such as:
- `termux-media-player`
- other installed Termux utilities

This is handled as part of the runtime tool system, not as fake text instructions.

---

## Project structure

```text
~/Termux-AI $ tree
.
├── CONTRIBUTING.md
├── LICENSE
├── README.md
├── api.keys
├── core
│   ├── __main__.py
│   ├── __pycache__
│   ├── interface.py
│   ├── llm_client.py
│   ├── memory_store.py
│   ├── permissions.py
│   ├── prompt.py
│   └── renderer.py
├── log.txt
├── memories.txt
└── workspace
```
---
## Components

### `interface.py`

Main terminal interface.

Responsibilities:
- user interaction
- conversation loop
- displaying output
- passing prompts into the model runtime

---

### `llm_client.py`

Core inference engine.

Handles:
- Gemini API access through the OpenAI-compatible endpoint
- tool calling
- retries
- overload recovery
- API key rotation
- automatic memory injection

---

### `memory_store.py`

Persistent memory subsystem.

Handles:
- parsing memory records
- semantic retrieval
- memory scoring
- top-k selection
- saving structured memories

---

### `permissions.py`

Sandbox enforcement layer.

Responsibilities:
- path checks
- workspace protection
- permission prompts for risky operations
- command validation

---

### `renderer.py`

ANSI markdown renderer.

Responsibilities:
- formatting responses
- code block display
- list formatting
- inline markdown styling
- terminal readability

---

### `prompt.py`

Behavior tuning layer.

Contains the system prompt and agent behavior rules.

---

## Setup

### Install Python dependencies

```bash
pkg install python rust mpv git
pip install openai edge-tts
```

### Clone the repo
```bash
git clone https://github.com/opsonusdh/Termux-AI
cd Termux-AI
```
For voice detection support you can add my another repo [Termux-STT](https://github.com/opsonusdh/Termux-STT/):
```bash
git clone https://github.com/opsonusdh/Termux-STT/
cd Termux-STT
bash setup.sh
cd ..
```
---
### Create API keys file

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
- temporary overloads happen
- provider-side failures appear

---

## Running

From the project root:

```bash
cd ~/Termux-AI
python core
```

Or run the module directly, depending on how your `__main__.py` is wired.

---

## Example session

### User

```text
YOU > create a small cli project
```

### Agent

The AI:
- inspects context
- plans the task
- calls tools internally
- creates files in `~/Termux-AI/workspace`
- validates outputs
- returns a formatted answer

---

## Security model

### Protected core

```text
~/Termux-AI/core
```

Contains the runtime logic and protected source code.

Modification requires explicit permission.

---

### Workspace

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

without requiring permission.

This allows the agent to:
- use `yt-dlp`
- process media with `ffmpeg`
- generate scripts
- create temporary build artifacts
- run local automation workflows

without repeatedly interrupting the user for confirmation.

---

### External system

The system may be inspected, but protected or risky actions require confirmation.

Examples:
- modifying system directories
- killing unrelated processes
- accessing privileged device APIs
- altering protected runtime files

---

## Current capabilities

- autonomous shell execution
- semantic memory retrieval
- long-term memory persistence
- markdown terminal rendering
- native tool calling
- multi-step reasoning
- filesystem management
- media playback
- Termux API integration
- retry-based recovery
- structured permissions
- API key rotation

---

## Current limitations

- no streaming output yet
- no async execution
- limited shell parsing heuristics
- long contexts can still grow large
- no vector embeddings yet
- no rollback or snapshot system

---

## Future ideas

- vector memory
- streaming responses
- background tasks
- voice interface
- plugin system
- execution rollback
- command confidence scoring
- multi-agent coordination

---

## Philosophy

This project is not trying to fake general intelligence.

It is a practical autonomous terminal agent designed to be:
- useful
- inspectable
- constrained
- recoverable
- extensible

The goal is controlled autonomy, not chaotic shell possession.

---

## License

MIT License

Use responsibly.

---

## Final note

- This project was started before OpenClaw existed.
- It has since evolved into a native tool-calling agent.
- It is intended to integrate with other Termux projects, including Termux-TUI.
- Improvements, suggestions, and testing are welcome.
