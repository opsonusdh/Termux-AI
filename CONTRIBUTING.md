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

## Running the Tests

The suite lives in `tests/` and runs with pytest:

```bash
pip install pytest
python3 -m pytest tests/ -q
```

A bare `python3 -m pytest` at the repository root runs the same suite: `pytest.ini` points it at `tests/`, so it never wanders into the git-ignored `workspace/` scratch area.

A few invariants the suite enforces by itself — a change that breaks them fails the run:

- `tests/conftest.py` stubs `plugin_runtime.setup`/`activate`/`deactivate`/`status`. **Always stub `setup` as well**: the real WhatsApp pairing path kills the device's live Termux-WP gateway, and a unit test that reached it took the running WhatsApp session down.
- Every advertised tool schema must have a dispatch route, every route must resolve to something callable, and the sub-agent worker registry must match the documented exclusions (`tests/test_tool_registry_audit.py`).
- Every route in the model registries must be free and verified for tool calling (`tests/test_subagent_tiers.py`).
- The version constant in `core/__main__.py` must match the release heading in `CHANGELOG.md` (`tests/test_version.py`).
- The session-memory module keeps its draft in `paths.WORKSPACE_DIR` and its saved sessions in `paths.LOGS_DIR`. A test must redirect both into `tmp_path` — a test that forgets writes into the real workspace (`tests/test_session_memory.py`).

The one-off audits and the live device checks live in `workspace/` (git-ignored). They are evidence for a change, not part of the repository — an invariant that must hold belongs in `tests/`.

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

- `config/config.json`
  Controls runtime settings such as STT path, TTS mode and `notify`. Its shape is
  defined in one place — `core/app_config.py` — so a new key is added there first.

- `core/models.py`
  Holds the provider endpoints and the model registries. Every route must be free and
  verified for tool calling.

- `memories.txt`
  Stores persistent instruction-style memory.

- `instructions/skills/`
  The modular skills the runtime actually loads. `instructions/skills/INDEX.md` routes
  a task to the skills it needs; the long-form manuals in `instructions/` are the
  readable versions of the same rules and have to be kept in step with them.

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
