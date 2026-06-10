# Release and Maintenance Workflow

This document defines how to keep Termux-AI maintainable while changing it. A working local fix is not enough if the setup path, documentation, templates, and verification path no longer match.

---

## Core Principle: Change the System, Not Just the File

Every meaningful change has adjacent obligations. A new tool may need schema, dispatch, docs, tests, config templates, and setup updates. A changed dependency may need setup changes and README changes. Treat the user-facing path as part of the change.

---

## 1. Before Maintenance Work

Establish the current state:

```bash
git status --short
find . -maxdepth 2 -type f | sort
```

For a targeted change, also identify:

- Owning module
- Public entry point
- Config files involved
- Runtime command that verifies it
- Documentation that mentions it

Do not clean unrelated dirty files unless the user asks. Existing modifications may belong to the user.

---

## 2. Change Impact Checklist

When changing behavior, check whether these need updates:

| Change | Adjacent files to consider |
|---|---|
| New Python dependency | `setup.sh`, `README.md`, import checks |
| New Termux package | `setup.sh`, `README.md`, permission notes |
| New config key | `config/config.json`, template/example docs, loader defaults |
| New secret format | `config/api.keys.template`, validation docs |
| New LLM tool | `core/tools.py`, `core/llm_client.py`, instructions, dispatch test |
| New hardware wrapper | `tools/`, `tools/__init__.py`, `core/tools.py`, docs |
| New agent state field | `agent/state_manager.py`, schema/default migration, tests |
| New log file | `paths.py`, `.gitignore`, privacy rules |

If a file is only informational and not needed for correctness, update it when the user-facing behavior would otherwise be misleading.

---

## 3. Version-Control Hygiene

Before editing:

- Run `git status --short`.
- Note unrelated dirty files.
- Avoid broad formatting of files outside the task.
- Do not revert user changes.

After editing:

- Review `git diff -- <files you changed>`.
- Verify only intended files changed.
- If generated files changed accidentally, either explain why they changed or restore only your generated changes when safe.

Never use destructive reset commands unless the user explicitly requests them.

---

## 4. Documentation Maintenance

Documentation should track behavior, not aspirations.

Update docs when:

- A setup command changes.
- A feature name, command, or file path changes.
- A new tool or workflow is added.
- A safety boundary changes.
- Troubleshooting steps changed because the actual failure mode changed.

Do not document features that are not implemented. If a feature is planned but not present, mark it explicitly as planned or omit it.

---

## 5. Config and Migration Rules

Runtime config should be backward-compatible where practical.

When adding a config field:

1. Provide a default in the loader.
2. Update examples/templates.
3. Validate type and allowed values.
4. Do not require users to manually edit existing configs unless unavoidable.
5. Report migration requirements plainly.

Never overwrite `config/api.keys` or user-specific config as part of a migration. Write a template or migration note instead.

---

## 6. Release Verification

Before declaring maintenance work complete, run verification that matches the changed surface.

Minimum docs-only verification:

```bash
find instructions -maxdepth 1 -type f -name '*.md' | sort
grep -n 'new_file_name.md' instructions/readme.md
```

Minimum Python code verification:

```bash
python3 -m py_compile core/*.py agent/*.py orchestration/*.py reflection/*.py tools/*.py
```

Minimum Node bridge verification:

```bash
node --check Termux-WP/bot.js
```

Minimum config verification:

```bash
python3 -c "import json; json.load(open('config/config.json')); print('config OK')"
```

Only run broader tests when the change touches broader behavior.

---

## 7. Packaging and Generated Artifacts

Keep generated artifacts out of source changes unless the user requested packaging.

Generated or environment-specific files include:

- `__pycache__/`
- `.wwebjs_auth/`
- `.wwebjs_cache/`
- `logs/`
- `workspace/` task outputs
- ZIP archives
- package manager lockfiles generated outside a dependency change

If packaging is requested, build from the current verified tree and mention exactly what artifact was produced.

---

## 8. Maintenance Report

A good final maintenance report contains:

- Files changed
- Behavior or documentation added
- Verification commands and outcomes
- Any existing dirty files left untouched
- Any follow-up that is required for the system to use the change

Keep it short. The report should prove the work is done, not narrate every step.

