---
name: memory_and_context
description: Memory and Context Management. This is a mandatory Termux-AI operational skill for tasks matching its scope.
---

## Termux-AI integration contract

- This skill is part of the repository's authoritative operating system. Apply it with the selected domain skills, not as an optional suggestion.
- Use the current project files and the tools actually available to Orion. Never invent a connector, package, API, or filesystem capability.
- Higher-priority instructions are the user request and runtime safety/permission controls.
- When this skill conflicts with a more specific task skill, follow the task-specific rule only where the conflict is explicit; preserve repository safety and verification rules.

# Memory and Context Management

This document defines how Orion uses conversation chunks, summaries, persistent memory, and indexed project knowledge. Good context management prevents two failures: forgetting important facts and acting on stale or imagined facts.

---

## Core Principle: Retrieve Before Relying

If a fact matters to the current task, verify it from the strongest available source before relying on it.

Order of trust:

1. Current file contents read in this session
2. Current command output
3. Retrieved raw chunk from conversation history
4. Persistent memory entry
5. Summary of an old chunk
6. Inference from architecture

Summaries are navigation aids, not proof. Raw files and raw chunks are proof.

---

## 1. Active Context Layout

The runtime keeps recent turns raw and older turns summarized. Treat this as a working set, not the full truth.

Typical layout:

```text
[system] Chunk 1: one-line summary
[system] Chunk 2: micro summary
[system] Chunk 3: short summary
[user/assistant/tool] recent raw turns
```

When a user references something from earlier in the conversation, do not guess from the summary if details matter. Use `list_chunks` to identify candidates and `retrieve_chunk(chunk_id)` to read the raw exchange.

---

## 2. When to Retrieve Chunks

Retrieve old raw chunks when:

- The user says "as before", "the previous fix", "that file we changed", or similar.
- A summary mentions a decision but not the exact constraint.
- You need a command output, error message, or file content from an older turn.
- A past user preference would change the implementation.
- You are about to continue a multi-turn task after interruption.

Do not retrieve chunks just to satisfy curiosity. Each retrieval should answer a concrete question.

---

## 3. Persistent Memory

`memories.txt` is long-lived and user-facing in effect. Use it for stable facts that should influence future sessions.

Good memories:

- "The user prefers concise final summaries."
- "For this project, run `python core` from the repository root."
- "The user wants WhatsApp auto-replies to avoid emojis."

Bad memories:

- Temporary task progress
- API keys or tokens
- Raw chat content
- Full stack traces
- Information that may expire quickly, such as today's price or a current server status

Before saving memory, ask whether the fact is stable and useful beyond the current task.

---

## 4. Indexed Project Knowledge

Use indexing for code and documentation that is too large to keep in active context.

Index when:

- The user asks broad questions across the repo.
- You need to find related functions across many files.
- A library of docs should be searchable later.

Do not index:

- Secret files in `config/`
- Auth/session folders such as `.wwebjs_auth/`
- Large generated caches
- Logs containing private message bodies unless the user explicitly requests it

After indexing, retrieve targeted snippets and then open the source file before editing. An index hit points to a file; it does not replace reading the file.

---

## 5. Working Scratch State

Use `workspace/reasoning_tmp.txt` for active multi-step tasks.

It should contain:

```markdown
# Current Task: <objective>

## Operation Type
targeted change / bulk transformation / diagnosis / investigation

## Known Facts
- Fact verified from file or command output

## Plan
- [x] Completed step
- [/] Current step
- [ ] Pending step

## Open Questions
- Question that cannot be resolved yet
```

Do not use persistent memory for task checkpoints. Scratch state belongs in `workspace/`; durable user preferences belong in `memories.txt`.

---

## 6. Avoiding Stale Context

A file, config, or external service can change after it was last read.

Re-read before acting when:

- The file is the target of a write.
- The file was modified by another tool, script, process, or user.
- The task resumed after a long pause.
- `git status` shows changes in the target file.
- The current action depends on exact line numbers or surrounding context.

It is acceptable to rely on a recent read for conceptual understanding, but not for a write that requires exact current contents.

---

## 7. Context Budget Discipline

Preserve context for information that changes decisions.

Prefer:

- Short summaries of long files after reading them
- Exact snippets only for affected sections
- Grep results for location discovery
- Raw chunk retrieval only when details matter

Avoid:

- Pasting entire files into conversation when a section is enough
- Re-reading unchanged files repeatedly
- Carrying old command output forward as if it were current state
- Saving long diagnostic dumps to persistent memory

Context is a resource. Spend it on facts that control the next action.

---

## 8. Claiming From Memory

When using remembered or retrieved information, identify its source.

Correct:

```text
The retrieved chunk says the user wanted concise WhatsApp replies. I have not checked the current config yet.
```

Incorrect:

```text
The config uses concise WhatsApp replies.
```

The first statement is about conversation history. The second is about current filesystem state. They are different claims and require different proof.

---

## 9. Closing a Conversation (`/session new`)

`/session new` is the `/clear` of Termux-AI. It wipes the chat history, the agent
history and the chunk context the model is fed; the next message starts from a blank
slate.

It deliberately does **not**:
- re-run the startup UI (no banner, no status row, no hint line),
- leave agent mode (`/agent` stays in agent mode),
- touch the preferences, the display state or the plugins.

The wipe is **durable**: the chunk store is not deleted — a **context boundary** is
moved forward and persisted (`data/context_session.json`), so the closed conversation
stops being injected into every request and does not come back after a restart. The
chunks stay retrievable with `retrieve_chunk`, so the model can still dig out earlier
work when the user asks about it. The session draft is archived as a normal saved
session rather than dropped.

**Do not "fix" a closed session by deleting chunks.** The boundary is the mechanism;
deleting the store destroys the history the model is still allowed to reach.

---

## 10. Reactive Context Compression & Archiving

### Reactive Triggering
- Compression triggers **reactively**, when the provider reports a context-length
  limit (or HTTP 405/413/422/431). A registry of "context limits per model" was a
  guess and was wrong about the endpoints that actually overflowed — the provider's
  own answer is the budget.
- The provider's stated ceiling is parsed from the error and used as the compression
  budget, with the system prompt, the tool catalogue and the output cap taken off the
  top.

### Archive Mechanics (`logs/context_archive.jsonl`)
- When older context is compressed, the raw reasoning, tool inputs and tool outputs
  are written to `logs/context_archive.jsonl` under a stable id (`A1`, `A2`, …).
- A marker left in the conversation names that id, so the model can page the raw text
  back with `retrieve_archive(id, offset, limit)` and list every segment with
  `list_archives()`. The digest is a summary; the archive is the evidence.

### Compression Invariants
1. **JSON Validity:** Tool-call `arguments` must stay parseable JSON. Truncate
   individual property values — never the argument blob.
2. **Tool Pairing:** A tool call and its result are never decoupled.
3. **No Unanswered Tool Calls:** Never leave an assistant tool call without its
   matching result turn.
4. **Compression Precedence:** History turns absorb compression before the live turn
   is altered. The system prompt, the recent turns and the prompt being answered are
   never reduced to a stub.