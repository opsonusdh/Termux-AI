---
name: core-bootstrap
description: Mandatory boot protocol for every Termux-AI task. Establishes instruction loading, capability discovery, operation classification, and evidence rules before any action.
---

# Core Bootstrap

This skill is mandatory for every task. Load it first.

## Before action
1. Read this skill using the file-reading tool in the current session.
2. Read `instructions/skills/INDEX.md` to determine applicable domain and core skills.
3. Read every selected skill before using a tool that acts on the task.
4. Never treat a path, prompt citation, remembered rule, or previous-session claim as proof that a skill was read.

## Before changing source
- Read the target file(s) in the current session.
- For coding/refactoring/tool changes, read `instructions/skills/core/coding/SKILL.md` before the first mutation.
- For non-trivial verification, also read `verification`; for complex diagnosis, read `reasoning`; for bulk changes, read `tool_efficiency`.

## Evidence discipline
- Current file read > current command output > retrieved raw context > memory > summary > inference.
- State assumptions as assumptions. Never claim a capability, file state, test result, or completed change that was not observed.

## Execution lifecycle
**Observe → classify → load skills → plan → execute → verify → repair → re-verify.**

A failure ends the current hypothesis, not the task. Diagnose the actual failure, change the approach materially, and rerun the relevant check. Do not repeat the same failing operation unchanged.