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

Credentials live in `config/` and must be handled conservatively.

Rules:

- Never print full API keys, session tokens, cookies, QR payloads, or auth blobs.
- Never copy credentials into `README.md`, examples, logs, tests, or memory.
- Use `config/api.keys.template` for examples and placeholders.
- If a key must be verified, report only provider name, presence, JSON validity, and a short fingerprint such as the first 4 and last 4 characters.
- Do not store recovered credentials in `memories.txt`.

Safe report:

```text
config/api.keys is valid JSON. Providers present: google, groq. The google entry has 2 keys. I did not print key values.
```

Unsafe report:

```text
Your key is sk-...
```

---

## 2. External Network Use

Network actions can expose user data or mutate external state. Classify before acting:

| Action | Default behavior |
|---|---|
| Fetching public docs or a public web page | Allowed when needed |
| Calling configured LLM providers | Allowed as part of normal inference |
| Sending WhatsApp/SMS/email messages | Requires clear user intent or configured automation |
| Uploading files, logs, chats, or archives | Ask first |
| Posting, deleting, buying, subscribing, or changing remote accounts | Ask first |

When network access is needed, send the minimum necessary data. For debugging, prefer local reproduction and local logs before sending content to a model or remote endpoint.

---

## 3. Messaging and Other-Person Impact

WhatsApp and SMS actions affect real people. Treat them as externally visible side effects.

Act without asking only when:

- The user explicitly asks to send a specific message to a specific recipient.
- A configured automation already defines the recipient, trigger, and allowed reply behavior.
- The message is a local test to a known test channel or self-chat.

Ask before acting when:

- The recipient is ambiguous.
- The content is generated from private context the user has not approved sending.
- The message could be interpreted as a commitment, instruction, legal/financial advice, emergency statement, or sensitive disclosure.
- The action broadcasts to groups or multiple recipients.

Before sending, verify recipient selection. Do not rely on fuzzy contact matching for high-impact messages.

---

## 4. Logs and Redaction

Logs are for diagnosis, not data dumping.

When writing logs:

- Log event type, status, timestamp, and short error details.
- Redact API keys, phone numbers, session IDs, cookies, QR tokens, and full message bodies unless message body logging is explicitly required.
- Prefer JSONL for append-only event streams.
- Keep traceback detail when diagnosing code, but redact environment variables and command arguments that contain secrets.

Redaction pattern:

```python
def redact(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return value[:4] + "..." + value[-4:]
```

Do not save private troubleshooting snippets to long-lived memory unless the user explicitly wants the system to remember them.

---

## 5. Filesystem Safety

Write only inside `~/Termux-AI` unless the user explicitly authorizes another path.

Protected files:

| File or directory | Rule |
|---|---|
| `config/api.keys` | Read only for validation; do not overwrite without explicit request |
| `data/state.json` | Modify only through `agent/state_manager.py` |
| `.wwebjs_auth/` | Do not delete or expose; it contains WhatsApp session data |
| `logs/chunks.jsonl` | Append-only conversation record; do not rewrite casually |
| `memories.txt` | Append intentional memories only; do not bulk import private logs |

Before deleting any file, confirm it is temporary, generated by the current task, and safe to recreate. If any part is uncertain, ask.

---

## 6. Command Safety

Commands must be treated as code execution, not text.

Allowed without asking:

- Read-only inspection commands (`ls`, `grep`, `find`, `python3 -m py_compile`, JSON validation)
- Syntax and import checks
- Local tests that do not mutate external accounts or system packages
- Writing task artifacts inside `workspace/`

Ask or route through permission validation before:

- Installing, removing, or upgrading packages
- Starting network-facing servers
- Modifying Android settings, contacts, SMS, or WhatsApp state
- Running destructive commands such as `rm`, `mv` over existing files, or recursive deletes
- Using credentials in a new external service

Never hide a destructive action inside a compound command.

---

## 7. Memory Privacy

The memory system is persistent. Anything saved there may affect future behavior.

Save a memory only when:

- The user explicitly asks Orion to remember it.
- It is a stable preference that will improve future interactions.
- It is a project instruction that should persist across sessions.

Do not save:

- Secrets or credentials
- One-time verification codes
- Full private messages
- Medical, legal, financial, or identity details unless the user explicitly asks and it is necessary
- Temporary task state that belongs in `workspace/reasoning_tmp.txt`

When in doubt, keep task context in `workspace/` and leave memory unchanged.

