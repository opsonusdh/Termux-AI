# Orion System Instruction Manuals

Welcome to the `instructions/` directory. This directory contains detailed, professional-grade guides, coding standards, and operational guidelines governing the behavior, development, and reasoning execution of the Orion architecture within the Termux environment.

---

## Directory Index

| Manual | Description |
| :--- | :--- |
| [**Coding Standards (`coding.md`)**](./coding.md) | Defines syntax standards, path handling via `paths.py`, multi-process constraints, error logging, and self-verification patterns under Termux. |
| [**Reasoning & Analysis (`reasoning.md`)**](./reasoning.md) | Outlines the cognitive lifecycle of executing tasks, decomposing requirements, using `reasoning_tmp.txt` tracking, and programmatically troubleshooting code errors. |
| [**Agentic Orchestration (`orchestration_workflows.md`)**](./orchestration_workflows.md) | Covers actor-like process delegation, non-blocking queue synchronization, lifecycle patterns of workers, and instructions on expanding capabilities. |
| [**Environment & Security (`environment_and_tools.md`)**](./environment_and_tools.md) | Instructions on safe execution boundaries, discovering local binaries/packages, utilizing Termux telemetry, and structuring tool wrappers. |

---

## Usage Instructions for Agents

1. **Before writing code:** Refer to `coding.md` to ensure your methods do not violate sandbox rules, resource utilization limits, or path structures.
2. **Before tackling complex prompts:** Read `reasoning.md` to establish your decomposition plan and initialize state tracking in `reasoning_tmp.txt`.
3. **When extending capabilities:** Follow the guidelines in `orchestration_workflows.md` and `environment_and_tools.md` to ensure correct integration, validation, and schema compliance.
