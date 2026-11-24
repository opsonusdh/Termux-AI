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

1. Do **Online Research** on the problem and fixes.
2. Formulate specific and testable hypotheses.
3. Prioritize the most likely or easiest to test.

---

### Phase 3: Planning & Execution

1. **Research:** Do online search and lookups for solutions.
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
