---
name: troubleshooting
description: Troubleshooting. This is a mandatory Termux-AI operational skill for tasks matching its scope.
---

## Termux-AI integration contract

- This skill is part of the repository's authoritative operating system. Apply it with the selected domain skills, not as an optional suggestion.
- Use the current project files and the tools actually available to Orion. Never invent a connector, package, API, or filesystem capability.
- Higher-priority instructions are the user request and runtime safety/permission controls.
- When this skill conflicts with a more specific task skill, follow the task-specific rule only where the conflict is explicit; preserve repository safety and verification rules.

# Troubleshooting

Use diagnosis, not repeated guessing.

## Python/import failures
Check the active working directory, `sys.path`, module resolution, and the exact import traceback. Confirm the intended file is the one being imported.

**Common Termux-AI import hazard:** `tools/` package and `core/tools.py` share the same import name.
```bash
python3 -c "import sys; sys.path.insert(0,'core'); import tools; print(tools.__file__)"
```
Expected: `.../Termux-AI/core/tools.py` — if it resolves to `tools/__init__.py`, fix bootstrap order: `core/` must be inserted before root.

## Tool failures
Identify whether the failure is permission, argument/schema, provider, timeout, or implementation. Inspect the tool result before retrying.

## Runtime mismatch
When an edited file appears not to be reflected at runtime, verify the running process and restart requirements. Do not assume a source edit hot-reloads an already imported module.

## State recovery
For interrupted multi-step tasks, read `workspace/reasoning_tmp.txt` and current state files before continuing. Re-read files changed by a previous partial operation.

## Verification
After fixing a failure, rerun the exact probe that demonstrated the failure and at least one adjacent regression test.