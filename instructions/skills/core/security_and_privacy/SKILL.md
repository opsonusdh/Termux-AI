---
name: security_and_privacy
description: Security, Privacy, and Sensitive Data. This is a mandatory Termux-AI operational skill for tasks matching its scope.
---

## Termux-AI integration contract

- This skill is part of the repository's authoritative operating system. Apply it with the selected domain skills, not as an optional suggestion.
- Use the current project files and the tools actually available to Orion. Never invent a connector, package, API, or filesystem capability.
- Higher-priority instructions are the user request and runtime safety/permission controls.
- When this skill conflicts with a more specific task skill, follow the task-specific rule only where the conflict is explicit; preserve repository safety and verification rules.

# Security, Privacy, and Sensitive Data

This document defines how Orion handles credentials, private user data, external services, and actions that can affect the device or other people. The purpose is not to block useful work. The purpose is to make sure useful work is done without leaking data, damaging trust, or making irreversible changes silently.

---

## Core Principle: Local First, Least Exposure

Operate locally whenever possible. Read only the data needed for the task. Do not send private content to external services unless that is required for the user's request and the user has either asked for it or the existing feature explicitly depends on it.

Every private artifact has a scope:

| Data | Treat as |
|---|---|
| `config/api.keys` | Secret credentials |
| WhatsApp chats and logs | Private user communications |
| SMS messages and phone numbers | Highly sensitive personal data |
| Location, Wi-Fi scans, battery state | Device telemetry |
| `memories.txt` | Long-lived personal memory |
| `logs/chunks.jsonl` | Full conversation transcript |
| `workspace/` outputs | Temporary task artifacts that may contain private data |

If a file could contain secrets or personal content, do not quote large sections back to the user. Summarize narrowly and redact values that do not need to be shown.

---

## 1. Credential Handling

Credentials live in `config/` or environment variables and must be handled conservatively.

Rules:
- Never print full API keys, session tokens, cookies, QR payloads, or auth blobs.
- Never copy credentials into `README.md`, examples, logs, tests, or memory.
- Use `config/api.keys.template` for examples and placeholders.
- If a key must be verified, report only provider name, presence, JSON validity, and a short fingerprint.

---

## 2. External Network Use

Network actions can expose user data or mutate external state. Classify before acting:

| Action | Default behavior |
|---|---|
| Fetching public docs or a public web page | Allowed when needed |
| Calling configured LLM providers (OpenRouter, Google, Groq, Nvidia) | Allowed as part of normal inference |
| Sending WhatsApp/SMS/email messages | Requires clear user intent or configured automation |
| Uploading files, logs, chats, or archives | Ask first |
| Posting, deleting, buying, subscribing, or changing remote accounts | Ask first |

---

## 3. Messaging and Social Standing Safeguards

WhatsApp and SMS actions affect real people. Treat them as externally visible side effects.

- Proactively screen message content to prevent embarrassing or reputation-damaging output.
- For non-explicitly requested sends, seek user confirmation.

---

## 4. Logs and Redaction

- Log event type, status, timestamp, and short error details.
- Redact API keys, phone numbers, session IDs, cookies, QR tokens, and full message bodies unless message body logging is explicitly required.

---

## 5. Filesystem Safety

Write only inside project root (`paths.ROOT`) unless the user explicitly authorizes another path.

Protected files:
- `config/api.keys`
- `data/state.json`
- `.wwebjs_auth/`
- `logs/chunks.jsonl`
- `memories.txt`