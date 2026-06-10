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

