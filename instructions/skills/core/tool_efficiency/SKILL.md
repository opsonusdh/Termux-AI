---
name: tool_efficiency
description: Tool Efficiency. This is a mandatory Termux-AI operational skill for tasks matching its scope.
---

## Termux-AI integration contract

- This skill is part of the repository's authoritative operating system. Apply it with the selected domain skills, not as an optional suggestion.
- Use the current project files and the tools actually available to Orion. Never invent a connector, package, API, or filesystem capability.
- Higher-priority instructions are the user request and runtime safety/permission controls.
- When this skill conflicts with a more specific task skill, follow the task-specific rule only where the conflict is explicit; preserve repository safety and verification rules.

# Tool Efficiency

Use the smallest number of reliable operations that fully establish the facts.

## Before action
Batch related reads/searches before edits. Read each target file in the current session before modifying it.

## Bulk work
For the same mechanical transformation appearing more than three times, use one script or batch operation, then verify counts and diffs.

## Probes
Prefer cheap factual probes (`grep`, `python3 -c`, `which`, `git status --short`) over speculative reasoning.

## Context
Do not paste entire large files into working context when targeted sections suffice. Use indexed knowledge for discovery, then open the real source before editing.

## Failure handling
Do not repeat an identical failed tool call unless the inputs or environment have materially changed.