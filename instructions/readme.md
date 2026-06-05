# Orion System Instruction Manuals

This directory contains the operational guidelines, coding standards, and reasoning frameworks that govern how Orion develops, executes tasks, and reasons about its own architecture.

---

## Manual Index

| Manual | Purpose |
| :--- | :--- |
| [**coding.md**](./coding.md) | Coding standards, module boundaries, `paths.py` usage, error handling, and testing patterns. |
| [**reasoning.md**](./reasoning.md) | Cognitive lifecycle for task execution: decomposition, state tracking in `workspace/`, troubleshooting protocol. |
| [**orchestration_workflows.md**](./orchestration_workflows.md) | Multi-process delegation via `orchestration/`, worker lifecycle, IPC queue protocol, and capability registration. |
| [**environment_and_tools.md**](./environment_and_tools.md) | Safe execution boundaries, Termux API usage, the `tools/` wrapper pattern, and package management. |

---

## Project Layout Quick Reference

```
~/Termux-AI/
├── core/              Runtime engine (interface, llm_client, context_manager, tools)
├── agent/             Planning, execution, validation, state management
├── orchestration/     Multi-process task delegation
├── reflection/        Execution logging and self-correction
├── tools/             Termux hardware API wrappers
├── instructions/      This directory — agent operational manuals
├── config/            api.keys, config.json, capability_registry.json  (gitignored)
├── data/              state.json, validator_schema.json
├── logs/              chunks.jsonl, chunk_summaries.json, reflection.jsonl  (gitignored)
├── workspace/         Scratch space for agent-generated files  (gitignored)
├── docs/patches/      Historical patch files
└── paths.py           Single source of truth for all file paths
```

The full annotated layout with data-flow diagrams is in `PROJECT_STRUCTURE.md` at the project root.

---

## Instructions for Agents

1. **Before writing code** — read `coding.md`. Verify module boundaries, import patterns, and `paths.py` usage before touching any file.
2. **Before tackling a complex task** — read `reasoning.md`. Decompose the goal, create a tracking file in `workspace/`, and work step by step.
3. **Before extending orchestration or capabilities** — read `orchestration_workflows.md` and `environment_and_tools.md` to confirm correct IPC usage and wrapper structure.
4. **Never hardcode paths** — always use `import paths` and the named constants (`paths.STATE_FILE`, `paths.API_KEYS_FILE`, etc.).
5. **Never modify `ask_ai()`** — it is a production-critical path. Agent context lives in `run_agent_step()`, not in the normal chat flow.
