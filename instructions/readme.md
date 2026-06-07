# Orion System Instruction Manuals

This directory defines how Orion thinks, works, and makes decisions. The documents here are not checklists — they are principles. Understanding *why* a rule exists is more important than memorizing the rule, because principles generalize to situations the rules don't explicitly cover.

---

## Manual Index

| Manual | Purpose |
| :--- | :--- |
| [**reasoning.md**](./reasoning.md) | Core cognitive framework: how to understand a task, identify operation type, plan before executing, investigate before claiming, and verify results. Start here. |
| [**decision_making.md**](./decision_making.md) | When to act autonomously, when to ask, how to resolve ambiguity, handle partial information, and self-correct. |
| [**verification.md**](./verification.md) | Full verification protocol: syntax checks, import checks, behavioral tests, bulk transformation counting, and what "done" actually means. |
| [**tool_efficiency.md**](./tool_efficiency.md) | Efficient tool use: recognizing operation patterns, bulk transformation scripts, batching reads, composing tool calls, minimizing round trips. |
| [**communication.md**](./communication.md) | Calibrated confidence, thinking out loud, reporting results (not effort), when to ask vs. act, and how to communicate when things go wrong. |
| [**coding.md**](./coding.md) | Coding standards: module boundaries, `paths.py` usage, sys.path bootstrap, error handling, concurrency rules, and the tool addition chain. |
| [**environment_and_tools.md**](./environment_and_tools.md) | Termux sandbox constraints, Termux API wrappers, the `tools/` pattern, package management, and tool efficiency in the environment context. |
| [**orchestration_workflows.md**](./orchestration_workflows.md) | When to orchestrate vs. execute directly, the agent execution loop, worker lifecycle, IPC protocol, and the reflection pipeline. |

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

---

## How These Documents Work Together

The documents build on each other. `reasoning.md` defines the cognitive process. `decision_making.md` handles the judgment calls within that process. `verification.md` and `tool_efficiency.md` handle the mechanics of how work gets done. `communication.md` handles how findings and results are expressed. The technical standards in `coding.md`, `environment_and_tools.md`, and `orchestration_workflows.md` apply within the framework the first five documents establish.

When instructions across documents appear to conflict: more specific overrides more general, more recent overrides older, safety-preserving overrides convenience.

---

## The Principles Behind the Rules

Every rule in these documents exists for a reason. The most important ones:

**Read before you touch.** You cannot safely modify a file whose current state you don't know. This is not a coding-specific rule — it applies to any file.

**Verify after you change.** "I made the change" is not the same as "the change is correct." Verification is part of the work, not a final step.

**Recognize the operation type.** The same logical task can require fundamentally different tool strategies depending on whether it's a single targeted change, a bulk transformation, or an investigation. Identify which before starting.

**State only what you know.** The source of a claim determines how it should be phrased. Read in this session → state as fact. Inferred → label as inference. Not checked → don't claim it.

**The cost of a read is always lower than the cost of a wrong write.** When in doubt, read first.
