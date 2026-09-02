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