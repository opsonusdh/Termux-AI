# Contributing to Termux-AI

Fork the repo  
Create a new branch  
Make changes  
Submit a pull request  

Autonomous chaos is allowed.  
Uncontrolled chaos is not.

Thanks for taking the time to contribute.

---

# Getting Started

## 1. Fork this repository

Fork the project to your GitHub account.

---

## 2. Clone your fork

```bash
git clone https://github.com/YOUR_USERNAME/Termux-AI.git
```

---

## 3. Enter the project directory

```bash
cd Termux-AI
```

---

## 4. Create a new branch

```bash
git checkout -b your-feature-name
```

Examples:

```bash
git checkout -b improve-sandbox
git checkout -b fix-command-parser
git checkout -b add-memory-tools
```

---

## 5. Make your changes

Keep changes focused and understandable.

One pull request should ideally contain:
- one feature
- one fix
- or one improvement area

Not:
- 17 unrelated rewrites
- dependency migrations
- a philosophical reinvention of shell execution

---

## 6. Push your branch

```bash
git push origin your-feature-name
```

---

## 7. Open a Pull Request

Describe:
- what changed
- why it changed
- possible side effects
- security implications if relevant

---

# Project Structure

```text
ai_root/
├── core/
│   ├── interface.py
│   ├── llm_client.py
│   ├── executor.py
│   ├── permissions.py
│   └── prompt.txt
├── workspace/
├── memories.txt
├── log.txt
└── api.keys
```

---

# Core Architecture

## `interface.py`
Main interaction loop.

Responsibilities:
- user interaction
- command extraction
- reasoning loop
- execution pipeline

---

## `llm_client.py`
Handles:
- Gemini SDK
- API key rotation
- retries
- tool calling

---

## `executor.py`
Responsible for:
- command execution
- logging
- timeout handling
- output collection

---

## `permissions.py`
Sandbox enforcement layer.

Controls:
- filesystem access
- protected directories
- dangerous commands
- permission gating

---

# Sandbox Rules

The AI may:
- read files globally
- write inside `~/ai_root`
- execute safe shell commands

The AI may NOT:
- modify `~/ai_root/core`
- modify system files
- run privileged commands
- bypass permission checks

without explicit user approval.

Any PR weakening these protections will likely be rejected.

Because “the AI deleted my environment” is not a bug report anyone enjoys reading.

---

# Adding Features

## Good Contribution Areas

### Sandbox Improvements
Examples:
- safer command parsing
- better path validation
- redirect handling

---

### AI Improvements
Examples:
- better context trimming
- memory summarization
- retry optimization
- command planning

---

### Tooling
Examples:
- internet search tools
- file editing tools
- execution previews
- safer write operations

---

### Documentation
Examples:
- setup guides
- architecture explanations
- examples
- troubleshooting

---

# Code Style

## General Rules

- Keep functions small
- Prefer readable logic
- Avoid unnecessary abstraction
- Comment unusual behavior
- Fail loudly when needed

---

## Avoid

```python
x = a if b else c if d else e
```

Prefer clarity over compactness.

Future contributors should not need archaeological training to understand the codebase.

---

# Dependencies

Avoid unnecessary dependencies.

Before adding a package:
- explain why it is needed
- explain why built-in modules are insufficient

Heavy dependencies for tiny tasks are discouraged.

---

# Testing

Test changes manually in Termux before opening a PR.

Recommended environment:
- Termux
- Python 3.11+
- Android API 34

---

# Installation Notes

Before installing dependencies in Termux:

```bash
export ANDROID_API=34
```

Install required package:

```bash
pip install google-genai
```

---

# Commit Messages

Good:

```text
fix heredoc execution handling
improve sandbox validation
add timeout retry logic
```

Bad:

```text
update
fix
works now
final_final_real
```

Git history is documentation, not a panic diary.

---

# Reporting Bugs

Open an issue and include:

- what you did
- expected behavior
- actual behavior
- logs/errors
- Python version
- Termux version

Useful bug reports save everyone time.

---

# Feature Requests

When suggesting features:
- explain the use case
- explain safety implications
- explain why it belongs in this project

Not every autonomous-agent idea is a good idea.

Some are just ransomware with extra steps.

---

# Security

This project intentionally limits the AI.

The system should remain:
- inspectable
- interruptible
- sandboxed
- recoverable

Contributions should preserve these principles.

---

# Final Notes

Small contributions are welcome:
- typo fixes
- docs
- cleanup
- bug fixes
- safety improvements

You do not need to rewrite the architecture to contribute meaningfully.

---

# License

By contributing, you agree that your contributions will be licensed under the project's license.
