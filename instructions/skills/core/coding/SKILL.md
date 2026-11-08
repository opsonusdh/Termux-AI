---
name: coding
description: Coding Standards. This is a mandatory Termux-AI operational skill for tasks matching its scope.
---

## Termux-AI integration contract

- This skill is part of the repository's authoritative operating system. Apply it with the selected domain skills, not as an optional suggestion.
- Use the current project files and the tools actually available to Orion. Never invent a connector, package, API, or filesystem capability.
- Higher-priority instructions are the user request and runtime safety/permission controls.
- When this skill conflicts with a more specific task skill, follow the task-specific rule only where the conflict is explicit; preserve repository safety and verification rules.

# Coding Standards

This document defines the coding standards, architectural rules, and safety guidelines for the Orion/Termux-AI codebase. The rules here exist for specific reasons — understanding those reasons is more important than memorizing the rules.

---

## 0. Instruction-Read Gate

This manual is executable policy for coding tasks, not background documentation.

Before any source-changing action:
1. Read `instructions/skills/core/coding/SKILL.md` in the current session with the file tool.
2. Read any additional required manuals identified by `core/PROMPT.md`.
3. Read every target file before editing it.
4. Record the verified target files and applicable manuals in the task scratchpad when the task is multi-step.

If `coding.md` has not been read in the current session, do not write, delete, or run a mutating command.

This gate is intentionally redundant with the system prompt: the prompt selects the policy, while this manual states the policy itself so the rule remains available if the task is resumed or the prompt is compressed.

---

## Before Touching Any File

Read the target file in full before making any modification. This is not optional and not limited to "coding tasks." It applies whenever you intend to write, edit, or delete any file.

**Why:** You cannot safely modify a file whose current state you don't know. Memory of what a file "probably contains" is not the same as seeing it. Files change between sessions. A write based on a stale mental model causes regressions that are hard to trace.

**Pre-edit Analysis:**
Before proposing any code change:
1. **Full File Read:** Always read the *entire* target file to understand its current state, context, and existing patterns.
2. **Dependency Check:** Identify all direct and indirect dependencies of the code you intend to modify. Consider how changes might impact other modules or components.
3. **Exact Line Number Identification:** For targeted edits, pinpoint the precise line numbers where changes need to occur.
4. **Related Files:** If the task involves a new feature or significant change, proactively identify other files (e.g., `paths.py`, `core/tools.py`, `core/llm_client.py`) that might require corresponding updates.

**Action-gated rule:** Before any `write_file`, `run_code` (that writes), or bash command that modifies a file — confirm you have read that file in the current session. If you haven't, read it first.

---

## Recognize the Operation Type Before Starting

Before writing a single line, identify whether this is:

- **A targeted change** — one specific location, one specific edit → read → edit → verify
- **A bulk transformation** — the same change in N places → read → write a script → run once → verify
- **Adding a new component** — new function, new tool, new wrapper → plan the full addition chain first

**The bulk rule:** If the same pattern needs to change in more than three places in a file, do not make three individual edits. Write a Python script or use `sed` to make all changes in one pass. It is faster, more consistent, and does not consume round trips.

---

## 1. Environment Constraints

The system runs **primarily in Termux/Android** with cross-platform compatibility for Linux, macOS, and Windows:

- **No global writable paths.** `/tmp`, `/var`, `/usr/local` are not reliably writable across all platforms. All paths must resolve through `paths.py` under the project root (`~/Termux-AI`).
- **IPC limitations.** Named pipes (FIFOs) and POSIX shared memory are restricted on Android. Use `multiprocessing.Queue` exclusively.
- **Resource pressure.** Keep threads minimal, always join subprocesses, and close file handles in `finally` blocks.
- **Cross-Platform Compatibility.** Never hardcode OS-specific commands without a platform check (`sys.platform == 'win32'`). For Termux, prefer `termux-*` commands via wrappers.

---

## 2. Module Boundaries

Every package has a strict responsibility. Crossing these boundaries causes regressions.

| Package | Responsibility | Import Pattern |
|:---|:---|:---|
| `core/` | LLM inference, tool dispatch, context management, chat loop | flat imports within core (`from tools import *`) |
| `agent/` | Task state, planning, execution, validation | `from agent import state_manager` |
| `orchestration/` | Multi-process delegation, IPC | `from orchestration import Manager, Worker` |
| `reflection/` | Execution logging, failure analysis, auto-retry | `from reflection import ReflectionLoop, attempt_correction` |
| `tools/` | Platform hardware API wrappers only (Termux API) | `from tools import wrapper_termux_battery_status` |
| `config/` | Secrets and runtime settings — no logic | read via `paths.API_KEYS_FILE`, `paths.CONFIG_FILE` |
| `data/` | Persistent state and schemas — no logic | read/write via `agent/state_manager.py` only |

### Absolute Rules

- **Do not wrap or replace `ask_ai()` and do not change its public return contract.** A bug fix inside `ask_ai()` is allowed only when the current task specifically requires it and the affected code is read and tested; prefer helper functions and provider-specific logic outside the public contract.
- **Never write to `data/state.json` directly.** Use `agent/state_manager.save_state()`.
- **Never append chat turns to the session `history` list.** `context_manager.build_history()` serves full conversation history from chunks.
- **Never summarize inside tool execution or between LLM calls.** `maybe_summarize_async()` runs only after `close_chunk()`.

---

## 3. sys.path Bootstrap

Every module that imports across packages must bootstrap `sys.path` the same way — `core/` first, root second:

```python
import os, sys
_CORE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_CORE)
if _CORE not in sys.path: sys.path.insert(0, _CORE)
if _ROOT not in sys.path: sys.path.insert(1, _ROOT)
```

This must appear **before** any project imports.

---

## 4. Centralized Path Resolution (`paths.py`)

`paths.py` is the **single source of truth** for every file path. Import it everywhere:

```python
import paths

state_file    = paths.STATE_FILE
api_keys      = paths.API_KEYS_FILE
config_file   = paths.CONFIG_FILE
logs_dir      = paths.LOGS_DIR
```

---

## 5. Error Handling & Defensive Programming

- **Never swallow exceptions silently.** No `except: pass` or `except Exception: pass` without at minimum a `print` or log entry.
- **Programmatic Exception Diagnosis:** Inspect errors programmatically.
- **Idempotence & Safety:** Every script or background logic block must be safe to execute multiple times.
- **Timeouts and Resource Limits:** When interacting with external systems or long-running operations, implement explicit timeouts.

---

## 6. Concurrency Rules

- **Main inference thread is single.** `ask_ai()` is synchronous.
- **Two background daemon threads permitted:** context summarizer (`maybe_summarize_async`) and reflection logger.
- **Thread-safe state access.** `context_manager.py` uses `_lock` around all reads/writes to shared state.
- **No shared mutable state between processes.** `orchestration/Manager` communicates exclusively through `multiprocessing.Queue`.

---

## 7. Adding a New LLM-Callable Tool

1. Write the implementation function in `core/tools.py`
2. Add the JSON schema to `TOOLS_DESCRIPTION` in `core/llm_client.py`
3. Add the dispatch `lambda` to `_dispatch_tool()` in `core/llm_client.py`
4. If the tool wraps a platform API: create wrapper first in `tools/`, call it from `core/tools.py`
5. Run syntax check on all modified files
6. Write a minimal dispatch test that calls `_dispatch_tool()` and asserts the return value

---

## 8. Testing & Verification

Every change **MUST** be verified:

```bash
# Syntax compilation check
python3 -m py_compile <path_to_file>

# Import check
python3 -c "import sys; sys.path.insert(0,'core'); import tools; print('OK')"
```

If any, **MUST** fix errors that are found.

---

## 9. Python 3.10+ Best Practices

- **Type Hints:** Use explicit typing and `X | Y` for Union types.
- **Pattern Matching:** Use `match ... case` statements where suitable.
- **Context Managers:** Always use `with` statements for resource management.

---

## 10. Module Import & Singleton Invariants

- **Importing Permissions:** `core/permissions.py` must always be imported as
  `from core import permissions` (or `from core.permissions import ...`), never as a
  top-level `import permissions` — that creates a duplicate module instance with
  isolated state, and the permission gate stops being the gate.
- **Module Shadowing (`tools`):** `core/tools.py` is imported as `tools`, shadowing the
  `tools/` package in `sys.modules`. Platform wrappers are loaded by file path
  (`spec_from_file_location`) or re-exported through `core/tools.py`.
- **Dual Module Registration:** Stateful core modules (context manager, context
  compressor) register themselves in `sys.modules` under both `core.<name>` and bare
  `<name>`, so two import styles cannot end up with two separate instances.

---

## 11. LLM Client, Model Registries & Output Separation

- **Type Hints & Docstrings:** Public functions carry explicit type hints and a
  docstring that says what the function guarantees.
- **Centralized Provider Endpoints:** Provider base URLs live only in `core/models.py`
  (`PROVIDERS`). Tools and agents look up `PROVIDERS[pid]["base_url"]` instead of
  hardcoding an endpoint.
- **Immutable Model Registries:** Never mutate `MODEL_SLOTS`, `AGENT_MODEL_SLOTS` or
  the image registry at runtime. A per-request override (a reduced `max_tokens`, a
  lowered reasoning effort) belongs in a **request-local** dictionary — one over-long
  prompt must not degrade that model for the rest of the process.
- **Free-only and tool-calling gates:** Every route in the chat rotation, the agent
  rotation and the sub-agent tiers must be servable without paid credits, and every
  model offered to a sub-AI must have verified tool calling. `free_route_violations()`
  and `model_supports_tools()` are the machine checks; the test suite fails on a
  violation rather than trusting a convention.
- **Key Exhaustion & Cooldown:** Persistent error tracking in `llm_client.py` rotates
  keys after 10 consecutive same-status failures (429 rate limits, for example) and
  applies a timed cooldown before retrying an exhausted key.
- **Working Trace Decoupling:** Internal narration never leaks into the answer the
  user reads. `core/llm_client.py` separates the working trace from the final answer
  and shows the trace in its own labelled, collapsable block.

---

## ## 12. Decision Model Chain (Wake Relevance & make_decision Tool)

A **decision model** is not a chat model.  It answers structured questions
about a piece of state — a yes/no question with a calibrated probability, a
pick from a set of options, a position on an ordered scale — instead of
generating prose.

Termux-AI routes these tasks through a dedicated chain in
`core.models.DECISION_MODEL_SLOTS`, separate from the chat and agent rotations:

1. **OpenRouter `respan/span-01-lite:free`** on the Decisions API
   (`POST /api/alpha/decisions`) — the only **free** decision model.  It is
   zero-priced (the API answers `usage.cost = 0`), answers in ~1 s, and
   returns a probability in [0, 1] for a *noul* question.  It is **not**
   reachable through `/chat/completions` — OpenRouter refuses it there
   ("is a decisions model and cannot be used with the chat/completions
   endpoint") — so it has its own caller.
2. **NVIDIA `nvidia/nemotron-3.5-bolt-e4b`** (4B, thinking off) — a fast free
   text model used as the *choice* classifier.
3. **Google `gemini-2.5-flash`** — the free-tier fallback for every question
   type.

**There is no NVIDIA decision model.**  NVIDIA exposes no decisions endpoint
(`/v1/decisions`, `/v1/alpha/decisions` and `/v1/decision` all answer HTTP
404) and its free catalogue serves chat and text models only.

### The three question types, and what the free model actually does

OpenRouter's Decisions API documents three question types — `noul` (yes/no,
answered with a probability), `choice` (a pick from named options, with a
distribution) and `score` (a position on an ordered scale, with a
distribution) — and one request may carry several questions at once.

| Type | What it asks | What the free model does |
|---|---|---|
| `noul` | a yes/no question, answered with a probability | **native** — the only type Respan implements |
| `choice` | a pick from named options, with a distribution | **not supported**: HTTP 400, "Respan only accepts noul questions whose instructions and criteria are present, plain strings" |
| `score` | a position on an ordered scale, with a distribution | **not supported**: the same HTTP 400 |

The 400 was verified live on 2026-11-06 against `respan/span-01-lite:free`,
`respan/span-01-lite` *and the paid `respan/span-01`*: the limit belongs to
the model family, not to the free tier.  The API itself does implement the
full typed contract — `typesafe/jev-1.13` answered both a choice question and
a score question live, each with a full distribution — but it is **paid**
(`usage.cost = 1.29e-05`) and `typesafe/jev-1.13:free` answers HTTP 404 "This
model is unavailable for free".  The free-only rule keeps it out of the
registry; it is recorded in `core.models.PAID_TYPED_DECISION_MODELS` so nobody
re-adds it by accident.  The typed code path in `core/tools.py` is written
against that contract and starts working the moment a free typed model
appears.

So the chain serves what the free model can, and says how the answer was
reached:

- **yes/no** → one `noul` question → the model's own probability.
- **a scale** (`scale=[...]`) → *derived*: one cumulative `noul` question per
  level ("is the level at least k?"), differenced into a distribution —
  `P(0) = 1 - P(>=1)`, `P(k) = P(>=k) - P(>=k+1)`.  All the questions travel
  in **one request**, so a whole scale costs one request against the daily
  allowance.  Measured live: the extremes land cleanly, the middle bands are
  fuzzy (4/6 on a hand-labelled set, both misses mid-level), so the result is
  reported as a *distribution*, not as one confident number.
- **a choice** (`options=[...]`) → the **chat routes** pick the option.  The
  free decision model is deliberately *not* asked to choose: measured live,
  one `noul` question per option picked the wrong option in 1 of 3 cases, and
  using it to verify the chat route's pick rated a wrong pick at **P=0.92**.
  Its opinion on a choice question is not trustworthy, so it is not used.

### Consumers

- `is_wake_relevant(text)` in `core/tools.py` — called from `sleep_mode()` once
  the wake word is heard.  Decision model first, Gemini second, **fail closed**
  (keep sleeping) when every route fails.
- `make_decision(question, options?, scale?, context?, criteria?)` — the
  agent-facing tool.  Returns `{answer, why, model, confidence}`; a scale
  question also returns `probabilities` and `score`.

### Contract notes (verified live)

- The `noul` contract: `instructions` is a plain string, `criteria` is an
  object of plain strings keyed `true`/`false`, and `state` is the content to
  evaluate — a string, or an object of the shape {input: [messages], output:
  message}.  An arbitrary object ({"customer_tier": ...}) is rejected with
  HTTP 400, so the chain always sends a string.
- Several questions in one request are answered in one call and count as one
  request: three `noul` questions in a single call came back with three
  probabilities, `usage.cost = 0`, in ~1 s.
- OpenRouter's free allowance is **50 requests/day per key**.  An exhausted key
  answers 429, a dead key answers 401, and the chain rotates keys around both.
- Gemini's OpenAI-compatible endpoint rejects NVIDIA's `enable_thinking`
  switch with HTTP 400, and returns no content at all when the token budget is
  below 64.  The chain enforces both.

### Usage

- Binary/yes-no: `make_decision(question="Should I do X?", context="...")`
- Choice: `make_decision(question="Which approach?", options=["A", "B"], context="...")`
- Ordered scale: `make_decision(question="How urgent is this?", scale=["low", "medium", "high"], context="...")`
- With criteria: `make_decision(question="...", criteria="prefer safety over speed")`

`confidence` is the confidence that was actually **measured**: the decision
model's own probability, the derived distribution's peak, or the number the
chat route stated.  A route that answered in words without stating one reports
`0.0`, meaning *not measured* — never a fabricated number.  The decision is
advisory: Orion keeps the final say.

**When adding a decision consumer:** use the shared helpers in `core/tools.py`
(`_decision_call_questions()`, `_decision_call_decisions()`,
`_decision_call_chat()`) rather than `ask_ai_simple()`, which infers the
provider from the model *name* and would send a decision model to the wrong
endpoint.
