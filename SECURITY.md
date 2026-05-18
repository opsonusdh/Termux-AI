# Security Policy

## Important Disclaimer

Termux-AI includes features for autonomous terminal operation, memory handling, validated shell execution, and optional voice interaction.

This project is intended for:
- personal learning
- local development
- controlled automation
- authorized security research only

Do not use the project on systems, networks, or devices you do not own or do not have explicit permission to test.

Humans keep treating automation like a substitute for judgment. It is not.

---

## Supported Versions

Only the current maintained version of the project is supported.

Older snapshots may still run, but they are not actively maintained and should not be treated as safe by default.

---

## Reporting a Vulnerability

If you discover a security issue, do **not** open a public issue.

Instead, report it privately through the repository Security tab if available, or contact the maintainer directly through GitHub.

Please include:
- a clear description of the issue
- steps to reproduce it
- likely impact
- any suggested fix

---

## What Counts as a Security Issue

Examples include:
- unsafe shell command construction
- command validation bypasses
- unauthorized file writes
- leaks involving `api.keys`
- unwanted access to `core/`
- broken permission boundaries
- unsafe logging or memory handling

---

## Sensitive Files and Paths

These paths deserve extra care:
- `api.keys`
- `core/`
- `Termux-STT/`
- `memories.txt`
- `log.txt`

Do not commit secrets, tokens, or personal data into the repository. Git history remembers everything like a vindictive archivist.

---

## Responsible Use Reminder

Termux-AI is a terminal assistant, not a liability shield. If a feature can affect a system, network, or account, use it responsibly and only where you have permission.
