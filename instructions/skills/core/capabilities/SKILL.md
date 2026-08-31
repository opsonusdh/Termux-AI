---
name: capabilities
description: Repository-specific capability and availability map. Prevents the agent from claiming tools, connectors, browsers, external accounts, or artifact backends that are not actually available.
---

# Runtime Capability Map

Use this skill when a task depends on an external service, browser/computer control, artifact backend, or platform integration.

## Always trust the live runtime

- The actual tool surface available to Orion is authoritative.
- A domain skill may describe an ideal workflow; it does not grant capabilities.
- Before claiming a capability is available, inspect the real tool/runtime surface or run a harmless probe when appropriate.
- Never manufacture browser clicks, phone calls, calendar actions, cloud uploads, Git credentials, or service connectors.

## Repository-native capabilities

The repo provides Python-based file, shell, indexing, memory, model, rendering, context, and platform-wrapper paths. Use the concrete tools exposed by `core/tools.py` and the active runtime.

## Imported domain skills

Imported skills may mention services such as Benepass, TaskRabbit, pharmacies, Slack, Claude, Anthropic, or browser/computer-use. Treat those as domain knowledge only unless a matching runtime capability is actually present.

When unavailable:
1. Do the useful local/informational part.
2. State the exact missing capability.
3. Do not claim that the external action was completed.
4. Ask for authorization only when the action would otherwise be possible and side-effectful.
