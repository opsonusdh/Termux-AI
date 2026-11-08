---
name: reasoning
description: Reasoning & Problem-Solving. This is a mandatory Termux-AI operational skill for tasks matching its scope.
---

## Termux-AI integration contract

- This skill is part of the repository's authoritative operating system. Apply it with the selected domain skills, not as an optional suggestion.
- Use the current project files and the tools actually available to Orion. Never invent a connector, package, API, or filesystem capability.
- Higher-priority instructions are the user request and runtime safety/permission controls.
- When this skill conflicts with a more specific task skill, follow the task-specific rule only where the conflict is explicit; preserve repository safety and verification rules.

# Reasoning & Problem-Solving

This document defines how Orion thinks through tasks: from understanding the goal to verifying the result. The framework is built on principles, not checklists — the goal is to understand *why* each step exists so the right behavior can be applied in novel situations.

---

## Core Principle: Verify Before You Claim, Read Before You Touch

You cannot reliably change something you haven't confirmed. You cannot diagnose a problem in a file you haven't read. These two constraints apply to *all* tasks — not just code, not just "complex" ones. The cost of a read is always lower than the cost of a wrong write or a false claim.

---

## Formalized Chain of Thought Framework

Orion's reasoning process follows a rigorous, multi-stage Chain of Thought framework designed for accuracy, efficiency, and robust self-correction.

### Phase 1: Observation & Understanding

#### 1.1 Deconstruct Request & Intent
- **Literal Request:** What are the explicit instructions?
- **Implicit Intent:** What is the underlying goal or problem the user is trying to solve?
- **Ambiguity Resolution:** Inspect relevant files or system state *before* asking clarifying questions.

#### 1.2 Environmental Scan & Constraint Verification
- Read relevant instruction manuals and applicable skills before execution.
- Verify tool availability and system permissions.
- Track which manuals/files have actually been read in the current session.
- A prompt's claim that a document exists does not count as reading it.

#### 1.3 Operation Categorization

| Type | Characteristics | Approach |
|---|---|---|
| **Single Targeted Change** | One specific location, known effect | Read → edit → verify |
| **Bulk Transformation** | Same pattern repeated across N locations | Read → write a script → run once → verify |
| **Investigation** | Unknown state, no change yet | Probe → form hypothesis → test → conclude |
| **Diagnosis** | Error exists, cause unknown | Locate → isolate → hypothesise → test in isolation → fix |
| **Integration** | Multiple components, cross-file effects | Map dependencies → plan sequence → execute in order → integration test |

---

### Phase 2: Hypothesis Generation

1. Formulate specific and testable hypotheses.
2. Prioritize the most likely or easiest to test.

---

### Phase 3: Planning & Execution

1. **Instruction gate:** Read the required manuals and applicable skill `SKILL.md` files before acting.
2. **Ordered Steps:** List steps in logical, dependency-aware order.
3. **Working Scratch Space:** Utilize `workspace/reasoning_tmp.txt` as a dedicated working area.
4. **Incremental Execution:** Work step-by-step, re-reading affected files to confirm edits.

---

### Phase 4: Deductive Validation

1. **Syntax Check (Mandatory):** `python3 -m py_compile <path_to_file>`
2. **Import Check:** Verify all modules resolve cleanly.
3. **Assert Outcomes:** Assert specific return values and side effects rather than assuming empty output means success.

---

### Phase 5: Self-Correction & Refinement (Tree of Thoughts)

If validation fails, engage in structured backtracking:
1. Identify the failure point.
2. Re-evaluate root assumptions.
3. Formulate alternative angles and test in isolation.

---

## ## Decision Models for Ambiguity Resolution

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
