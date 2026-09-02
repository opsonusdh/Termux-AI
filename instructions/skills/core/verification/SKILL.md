---
name: verification
description: Verification Workflow. This is a mandatory Termux-AI operational skill for tasks matching its scope.
---

## Termux-AI integration contract

- This skill is part of the repository's authoritative operating system. Apply it with the selected domain skills, not as an optional suggestion.
- Use the current project files and the tools actually available to Orion. Never invent a connector, package, API, or filesystem capability.
- Higher-priority instructions are the user request and runtime safety/permission controls.
- When this skill conflicts with a more specific task skill, follow the task-specific rule only where the conflict is explicit; preserve repository safety and verification rules.

# Verification Workflow

Verification is part of the change, not a final courtesy.

## Minimum
For every Python edit:
1. Re-read the affected file after the write.
2. Run `python3 -m py_compile` on every modified Python file.
3. Run an import check for the affected module path.
4. Run the smallest behavioral test that proves the changed behavior.
5. Inspect the command exit code and relevant output.

## Broader changes
When a change crosses modules, test the integration path, not only individual functions. For bulk edits, assert the expected number of changes.

## Failure rule
If verification fails, identify the exact failure, correct the root cause, and repeat the failed verification. Never report success based only on a successful write.