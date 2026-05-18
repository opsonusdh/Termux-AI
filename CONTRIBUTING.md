# Contributing to Termux-AI

Thanks for helping improve **Termux-AI** and its terminal agent, **Orion**. This project is built for controlled autonomy inside Termux, not for random chaos disguised as productivity.

## Getting Started

1. Fork the repository
2. Create a branch for your change
3. Make the smallest clean change that solves the problem
4. Test it in Termux
5. Open a pull request

---

## What This Project Cares About

Termux-AI is centered around:
- terminal-based orchestration
- validated command execution
- memory retrieval
- voice input and wake-word handling
- safe interaction with local files
- clean terminal rendering

Changes should respect the existing architecture instead of wrestling it into abstraction soup.

---

## Before You Submit

Please check that your change:
- works on a real Termux environment
- does not break `core/interface.py`, `core/tools.py`, or `core/permissions.py`
- does not weaken command validation
- does not write into protected areas unless intended
- keeps behavior understandable and predictable

---

## Code Style

- Keep code readable and explicit
- Prefer small functions over clever sprawl
- Avoid unnecessary dependencies
- Keep shell commands validated and controlled
- Do not add silent side effects
- Match the project’s current tone and structure where reasonable

---

## Working With Core Components

Important files:

- `core/interface.py`
  Handles the user loop, config loading, and voice mode.

- `core/tools.py`
  Handles tool dispatch, memory, logging, and external actions.

- `core/permissions.py`
  Defines what the agent may do without asking first.

- `config.json`
  Controls runtime settings such as STT path and TTS mode.

- `memories.txt`
  Stores persistent instruction-style memory.

If your change touches these, test carefully. Autonomous systems already have enough opportunities to embarrass everyone.

---

## Good Pull Requests

A good pull request is:
- focused on one thing
- described clearly
- tested with real input
- free of unrelated edits
- easy to review

If fixing a bug, include:
- what broke
- how to reproduce it
- what changed
- how you tested it

---

## Reporting Bugs

Open an issue and include:
- what you tried
- what you expected
- what happened instead
- relevant terminal output
- your Termux and Python versions

---

## Suggesting Features

Feature requests are welcome, especially if they improve:
- memory behavior
- permission handling
- terminal rendering
- voice interaction
- tool reliability
- workspace handling

If a feature adds complexity, explain why it is worth it. Complexity breeds in dark corners and undocumented helper functions.

---

## Community Expectations

Be respectful. Be specific. Be useful.

No harassment, no personal attacks, no drive-by nonsense.

---

## License

By contributing, you agree that your changes may be distributed under the project’s license.
