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
- leaks involving `api.keys`, the OAuth client secret or a refresh token
- a confidential file being read, uploaded or shared without the gate
- a WhatsApp pairing QR or session token reaching the terminal or a log
- unwanted access to `core/`
- broken permission boundaries
- unsafe logging or memory handling

---

## Sensitive Files and Paths

These paths deserve extra care:
- `config/api.keys` — every provider key
- `config/google_oauth.json` — the Google OAuth client secret
- `data/google_tokens/` — long-lived refresh tokens for Gmail, Calendar and Drive
- `data/google_pending_auth.json` — a live PKCE verifier
- `data/cli_history` — the prompt history; a pasted credential ends up here
- `data/spill/` — oversized tool results, which may quote a file verbatim
- `core/`
- `Termux-STT/`
- `memories.txt`
- `logs/` — conversations, tool output and the compressed-context archive
- `logs/log.txt`
- `.wwebjs_auth/` — the paired WhatsApp session

The credential files are git-ignored, and they are protected in **every** permission
mode — including autonomous mode. A WhatsApp pairing code is a live secret too: it is
captured and drawn only when the user asks for it, never printed on its own.

Beyond these known paths, the permission gate recognises a confidential file by
**its name or its content** — a `.env`, an `id_rsa`, a `credentials.json`, a file
that happens to hold a provider token under an innocent name. The gate asks before
reading one, and refuses outright in autonomous mode (nobody is there to answer).
Uploads and shares go through the same gate, and `search_in_files` / `index_files`
skip confidential files rather than grepping them.

Do not commit secrets, tokens, or personal data into the repository. Git history remembers everything like a vindictive archivist.

---

## Responsible Use Reminder

Termux-AI is a terminal assistant, not a liability shield. If a feature can affect a system, network, or account, use it responsibly and only where you have permission.
