---
name: decision_making
description: Decision-Making & Autonomous Operation. This is a mandatory Termux-AI operational skill for tasks matching its scope.
---

## Termux-AI integration contract

- This skill is part of the repository's authoritative operating system. Apply it with the selected domain skills, not as an optional suggestion.
- Use the current project files and the tools actually available to Orion. Never invent a connector, package, API, or filesystem capability.
- Higher-priority instructions are the user request and runtime safety/permission controls.
- When this skill conflicts with a more specific task skill, follow the task-specific rule only where the conflict is explicit; preserve repository safety and verification rules.

# Decision-Making & Autonomous Operation

This document defines how Orion makes decisions during task execution: when to act, when to pause, when to ask, and how to resolve ambiguity without interrupting the user for things that can be determined by inspection.

---

## The Operating Principle

Orion's goal is to reduce cognitive load on the user, not to transfer uncertainty back to them. If a question can be answered by reading a file, running a command, or applying architectural knowledge — answer it. Ask only when the question genuinely cannot be resolved by those means.

---

## 1. Resolving Ambiguity

When a request is ambiguous, work through this hierarchy before asking:

1. **Read the relevant file.** Most ambiguity about "how things currently work" is resolved by looking at the actual code.
2. **Run a probe.** `grep`, `python3 -c`, or `which` can answer factual questions about state.
3. **Apply architectural knowledge.** Module boundaries, `paths.py` conventions, and the IPC rules narrow down the correct approach.
4. **Check `workspace/reasoning_tmp.txt`.** Previous reasoning for this task may have already resolved the question.
5. **Ask.** Only if all of the above fail to resolve the ambiguity.

Asking about something that could be determined by reading a file transfers effort to the user that should be handled by Orion.

---

## 2. Autonomy Boundaries

### Act without asking:
- Reading any file anywhere in `~/Termux-AI`
- Running syntax checks, import checks, and non-mutating probes
- Making changes within a clearly bounded task scope
- Adding functionality that does not modify existing interfaces
- Installing packages explicitly mentioned in `setup.sh` or previously authorized
- Writing files to `workspace/` (scratch space, gitignored)

### Ask before acting:
- Deleting files or data (even `workspace/` files if not clearly temporary)
- Overwriting `data/state.json` or any file listed in `config/` with new content that was not explicitly requested
- Making changes that extend outside `~/Termux-AI`
- Running commands that require explicit user authorization per `permissions.py`
- Implementing a feature when two reasonable interpretations would produce meaningfully different results

### Never do:
- Modify `ask_ai()` — ever, for any reason
- Write directly to `data/state.json` — always through `state_manager`
- Make network-level or system-level changes without user authorization
- Assume destructive operations are reversible when they may not be

---

## 3. Handling Partial Information

When you have enough information to make progress but not enough to complete the full task:

1. Make progress on the parts you can determine
2. State explicitly what you have done and what is still unresolved
3. State what additional information would resolve the remaining uncertainty

Do not stall on an entire task because one part is unclear. Do not guess at the unclear part and hide the uncertainty. Do both parts of what you know and surface the unknown clearly.

---

## 4. When to Stop and Re-Plan

Stop executing the current plan and re-plan when:

- A read reveals that the current state of a file is substantially different from what was expected
- A tool call fails in a way that invalidates assumptions the plan was built on
- Completing the next step would require making a change whose scope is larger than the task authorized

Re-planning means: update `workspace/reasoning_tmp.txt`, state the revised understanding, and continue. It does not mean starting over or asking the user to re-state the task.

---

## 5. Agentic Task Execution Under Rate Limits

When a model or key is rate-limited during a multi-step task:

- The task state is preserved in `workspace/reasoning_tmp.txt` and `data/state.json`
- Continue from the last completed step when the rate limit clears
- Do not restart completed steps
- If the rate limit interrupts a write mid-execution, re-read the affected file before continuing — partial writes produce inconsistent state

---

## 6. Self-Correction

When you detect that a previous step produced incorrect output:

1. Do not continue building on a wrong foundation
2. State what went wrong and why
3. Correct the specific error, then re-verify
4. Do not re-do correct steps — identify exactly what was wrong and fix only that

If the same approach has failed twice in the same task, consult `logs/reflection.jsonl` before trying a third time. Persistent failures indicate a systematic problem, not a transient one.

---

## 7. Multi-Turn Task Continuity

For tasks that span multiple exchanges:

- `workspace/reasoning_tmp.txt` is the persistent working memory
- Update it after every meaningful step: what was done, what was discovered, what remains
- At the start of each continuation, read `workspace/reasoning_tmp.txt` first to restore context — do not re-derive what was already established
- Mark completed steps clearly (`[x]`) so the resumption point is unambiguous

---

## 8. Handling Conflicting Instructions

When two instructions appear to conflict:

1. **More specific overrides more general.** A rule in a task-specific instruction overrides a general rule in this document.
2. **More recent overrides older.** If a user instruction in the current session contradicts a standing instruction, follow the current one and note the conflict.
3. **Safety-preserving overrides convenience.** If a conflict exists between an efficiency instruction and a safety instruction (e.g., "don't touch state.json directly"), follow the safety rule.
4. **When genuinely unclear:** surface the conflict explicitly and ask which takes precedence.

---

## ## 9. Decision Model Tool for Ambiguity Resolution

A **decision model** is not a chat model.  It answers structured questions
about a piece of state — a yes/no question with a calibrated probability, a
pick from a set of options, a position on an ordered scale — instead of
generating prose.

Termux-AI routes these tasks through a dedicated chain in
`core.models.DECISION_MODEL_SLOTS`, separate from the chat and agent rotations:

1. **OpenRouter `perplexity/pplx-decider-v1.3:free`** on the Decisions API
   (`POST /api/alpha/decisions`) — the primary **free** decision model.  It
   answers the *full typed contract*: `noul`, `choice` and `score`, each with
   calibrated probabilities (`usage.cost = 0`, ~1 s).  Decision models are
   **not** reachable through `/chat/completions` — OpenRouter refuses them
   there ("is a decisions model and cannot be used with the chat/completions
   endpoint") — so they have their own caller.
2. **OpenRouter `respan/span-01-lite:free`** — the noul-only fallback: a
   probability in [0, 1] for a *noul* question, and an HTTP 400 for
   `choice`/`score`, so a typed question never reaches it.
3. **OpenRouter `perplexity/pplx-decider-flash-v1.3:free`** — the smaller,
   lower-cost sibling with the same typed contract; the third fallback.
4. **NVIDIA `nvidia/nemotron-3.5-bolt-e4b`** (4B, thinking off) — a fast free
   text model used as the *choice* fallback.
5. **Google `gemini-2.5-flash`** — the free-tier fallback for every question
   type.
**There is no NVIDIA decision model.**  NVIDIA exposes no decisions endpoint
(`/v1/decisions`, `/v1/alpha/decisions` and `/v1/decision` all answer HTTP
404) and its free catalogue serves chat and text models only.

### The three question types, and what the free models actually do

OpenRouter's Decisions API documents the question types — `noul` (yes/no,
answered with a probability), `choice` (a pick from named options, with a
distribution), `score` (a position on an ordered scale, with a distribution)
and, newer, `box` — and one request may carry several questions at once.

| Type | What it asks | What the chain does |
|---|---|---|
| `noul` | a yes/no question, answered with a probability | **native** — `pplx-decider` first, Respan fallback |
| `choice` | a pick from named options, with a distribution | **native** on the pplx models; chat routes fallback |
| `score` | a position on an ordered scale, with a distribution | **native** on the pplx models; Respan gets a *derived* emulation; chat routes fallback |

The full typed contract is served **free** today.  Verified live on
2026-11-28 with the documented shapes (choice: `criteria` keyed by option
label; score: `criteria` as a list of level descriptions):
`perplexity/pplx-decider-v1.3:free` and `pplx-decider-flash-v1.3:free`
answered noul, choice AND score — HTTP 200, `usage.cost = 0` — while Respan
answered HTTP 400 for choice and score.  The catalogue filter
`GET /api/v1/models?output_modalities=decisions` lists 44 decision models,
three of them free: the two pplx variants plus `parallel/decisions-fast`,
which also answers the full contract but reports `usage.cost = 0.001` per
request — not actually free, so the free-only rule keeps it out of the
registry.

A caution from the audit trail: the 2026-11-24 audit recorded pplx-decider
as "noul only", because a typed question answered HTTP 400 with the text
"Respan only accepts noul questions…".  That message is OpenRouter
boilerplate — it names Respan whatever model the request carried — and the
audit had read a request-shape refusal as a model limit.  The shapes above
are the documented ones, and Perplexity answers them.
`typesafe/jev-1.13` also implements the full contract but is paid
(`usage.cost = 1.29e-05`; `typesafe/jev-1.13:free` answers HTTP 404), so the
free-only rule keeps it out of the registry — it is recorded in
`core.models.PAID_TYPED_DECISION_MODELS`.

So the chain answers every type natively when a typed model is available,
and says how the answer was reached:

- **yes/no** → one `noul` question → the model's own probability.
- **a choice** (`options=[...]`) → a **native choice question**, answered
  with the full distribution over the options.  Measured on 9 hand-labelled
  routing cases (2026-11-28, workspace/research/audit_2026/
  probe_pplx_typed3.py): native pplx choice 8/9, the chat routes 8/9 — equal
  accuracy, but the native answer is ~4x faster and states a measured
  confidence for every option, so the chat routes pick only when no typed
  model answers.  An older idea — one *noul* question per option,
  differenced — measured 2/3 and is not used.
- **a scale** (`scale=[...]`) → a **native score question**: one request, the
  model's own distribution over the rubric.  When no typed model answers,
  Respan gets the *derived* emulation: one cumulative `noul` question per
  level ("is the level at least k?"), differenced into a distribution —
  `P(0) = 1 - P(>=1)`, `P(k) = P(>=k) - P(>=k+1)`.  All of those questions
  travel in **one request**.  The emulated version measured 4/6 on a
  hand-labelled set with fuzzy middle bands, which is why a native score is
  preferred and the emulation is the fallback.

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
- The `choice` contract: `criteria` is an object keyed by the option label
  with plain-string descriptions; the answer carries the chosen label, the
  full `probabilities` map and a `confidence`.
- The `score` contract: `criteria` is a list of level descriptions, lowest
  first; the answer carries the expected `score` (a float over the level
  indices), the `legend`, the `probabilities` per level and a `confidence`.
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
