# Terminal Autonomous AI Agent (Termux)

A sandboxed autonomous AI agent for Termux powered by Gemini.

The agent can:
- reason step-by-step
- execute shell commands
- inspect files
- create projects
- search the internet
- maintain memory
- operate inside a restricted filesystem sandbox

Unlike normal chatbots, this system behaves like a terminal-capable reasoning agent with controlled autonomy.

---

# Features

## Autonomous Shell Execution
The AI can generate and execute bash commands automatically.

Example:

```bash
grep -R "exam" ~/ai_root
```

Command outputs are fed back into the reasoning loop.

---

## Sandboxed File System

The AI operates inside:

```text
~/ai_root
```

### Permissions

| Action | Allowed |
|---|---|
| Read any file | ✅ |
| Write inside `~/ai_root` | ✅ |
| Modify `~/ai_root/core` | ❌ Requires permission |
| Modify outside `~/ai_root` | ❌ Requires permission |

This prevents accidental self-modification and dangerous system changes.

---

## Multi-Step Reasoning Loop

The AI can:
1. think
2. execute commands
3. inspect outputs
4. retry
5. refine answers

until it becomes confident enough to respond.

---

## Gemini SDK Integration

Uses:
- `google-genai`
- Gemini 2.5 Flash

Supports:
- API key rotation
- retry handling
- overload recovery
- tool calling

---

## Memory System

### `memories.txt`
Persistent long-term memory.

Stores:
- summaries
- important discoveries
- reusable information

### `log.txt`
Stores:
- commands
- outputs
- execution history

---

# Project Structure

```text
~/ai_root/
├── api.keys
├── memories.txt
├── log.txt
├── core/
│   ├── interface.py
│   ├── llm_client.py
│   ├── executor.py
│   ├── permissions.py
│   └── prompt.py
├── workspace/
│   ├── downloads/
│   └── temp/
```

---

# Components

## `interface.py`

Main terminal interface.

Responsibilities:
- user interaction
- AI loop
- command extraction
- feeding outputs back to the model

---

## `llm_client.py`

Handles:
- Gemini API
- SDK integration
- retries
- tool calling
- key rotation

---

## `executor.py`

Executes shell commands safely.

Features:
- timeout handling
- logging
- execution output capture
- sandbox validation

---

## `permissions.py`

Sandbox enforcement layer.

Blocks:
- privileged commands
- interactive tools
- writes outside sandbox
- modifications to `core/`

---

## `prompt.txt`

Behavior tuning layer for the AI.

Can be modified without changing source code.

---

# Installation

## Install Python

```bash
pkg install python, rust
```

---

## Install dependencies

```bash
export ANDROID_API_LEVEL=34
pip install google-genai
```

---

# Setup

## Create API keys file

Create:

```text
~/ai_root/api.keys
```

Example:

```text
AIza....
AIza....
```

One key per line.

---

# Running

Move into the core directory:

```bash
cd ~/ai_root/core
```

Start the agent:

```bash
python interface.py
```

---

# Example Session

## User

```text
YOU > create a cli snake game
```

## AI

```text
[Thinking(1)]
```

```bash
mkdir -p ~/ai_root/game
```

```text
[Executing commands]
```

```text
<<<END_OF_COMMAND_OUTPUT>>>
```

The AI continues building the project autonomously.

---

# Safety Model

The project uses a layered security model.

## Trust Zones

### 1. `core/`
Protected system logic.

AI cannot modify without permission.

---

### 2. `ai_root/`
Writable workspace.

AI can:
- create files
- run code
- manage projects

---

### 3. System
Read-only external environment.

AI may inspect files but cannot modify them automatically.

---

# Design Goals

- autonomous but controlled
- inspectable execution
- terminal-native
- lightweight
- local-first
- extensible
- safe enough for experimentation

---

# Current Limitations

- no streaming responses
- no async execution
- limited shell parsing
- context can grow large
- no token optimization yet
- no GUI

---

# Future Ideas

- web search tools
- package install approval system
- self-repair mode
- plugin system
- voice interface
- vector memory
- multi-agent support
- command confidence scoring
- execution rollback system

---

# Philosophy

This project is not trying to simulate AGI.

It is a practical autonomous terminal agent:
- constrained
- inspectable
- useful
- recoverable when it inevitably does something creatively stupid

Because giving an LLM shell access without boundaries is less “innovation” and more “digital natural selection”.

---

# License

MIT License

Use responsibly.
```
