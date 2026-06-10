# Troubleshooting Runbook

This document provides a practical runbook for diagnosing failures in Termux-AI. The goal is to move from symptom to root cause without guessing, repeated retries, or unrelated rewrites.

---

## Core Principle: Preserve the Evidence

An error message is data. Do not overwrite logs, clear state, reinstall packages, or delete sessions before capturing the evidence needed to understand the failure.

Troubleshooting order:

1. Capture the exact symptom.
2. Locate the failing component.
3. Reproduce the smallest failing path.
4. Form a specific hypothesis.
5. Test that hypothesis.
6. Fix the smallest cause.
7. Verify the full path.

---

## 1. First Triage

Start with these questions:

- What command, tool, or user action triggered the failure?
- Is this a Python error, Node error, Termux API error, provider/API error, permission error, or logic error?
- Did the failure happen before execution, during execution, or during verification?
- Is there a recent code/config change in the affected area?
- Is the affected file or state currently dirty in `git status`?

Then gather the minimum evidence:

```bash
git status --short
python3 --version
pwd
```

For Python syntax issues:

```bash
python3 -c "import ast; ast.parse(open('path/to/file.py').read()); print('syntax OK')"
```

For JSON:

```bash
python3 -c "import json; json.load(open('path/to/file.json')); print('json OK')"
```

---

## 2. Error Taxonomy

| Symptom | Likely class | First check |
|---|---|---|
| `SyntaxError` | Broken edit | AST parse target file |
| `ModuleNotFoundError` | Import path or missing dependency | `sys.path`, package install, file exists |
| `Permission denied` | Android or filesystem sandbox | Path location and command permissions |
| Tool call requested but not executed | Dispatch/schema mismatch | `TOOLS_DESCRIPTION` and `_dispatch_tool()` |
| State lost after restart | State persistence bug | `data/state.json` through state manager |
| WhatsApp QR/auth loop | Session or bridge issue | Termux-WP logs and `.wwebjs_auth/` |
| API rate limit | Provider/key rotation | LLM client provider logs |
| Context references missing | Chunk retrieval issue | `list_chunks`, `retrieve_chunk` |

Do not jump from symptom to rewrite. Identify the class first.

---

## 3. Python Import Failures

This project has a known import hazard: `tools/` package and `core/tools.py` share the same import name.

Check:

```bash
python3 -c "import sys; sys.path.insert(0,'core'); import tools; print(tools.__file__)"
```

Expected when importing LLM-callable tools:

```text
.../Termux-AI/core/tools.py
```

If it resolves to `.../Termux-AI/tools/__init__.py`, fix the bootstrap order: `core/` must be inserted before root.

---

## 4. Tool Dispatch Failures

When a tool exists but the model cannot use it, verify the full addition chain:

1. Implementation function exists in `core/tools.py`.
2. Tool schema exists in `TOOLS_DESCRIPTION` in `core/llm_client.py`.
3. `_dispatch_tool()` maps the schema name to the implementation.
4. Argument names in the schema match the function signature.
5. Return type is serializable and user-readable.

Minimal dispatch test:

```bash
python3 -c "
import sys, json
sys.path.insert(0, 'core')
sys.path.insert(1, '.')
from llm_client import _dispatch_tool
call = {'function': {'name': 'TOOL_NAME', 'arguments': '{}'}}
result = _dispatch_tool(call)
print(type(result).__name__, str(result)[:120])
"
```

Replace `TOOL_NAME` and arguments with the actual tool under test.

---

## 5. State and Agent Loop Failures

Do not edit `data/state.json` directly. Diagnose through `agent/state_manager.py`.

Check:

- Is `active_task_id` set?
- Does `cursor` point to an existing task?
- Are task IDs unique?
- Are pending, active, completed, and failed statuses consistent?
- Was `worker_output` persisted before critic execution?
- Was `critic_output` persisted before final status update?

If state is malformed, write a repair function in state manager or a one-off script that imports state manager, validates the state, writes a backup, repairs the specific issue, then saves through the supported path.

---

## 6. WhatsApp and Node Failures

Check in order:

```bash
node --version
ls Termux-WP
```

Then inspect the latest relevant bridge log rather than reinstalling immediately.

Likely causes:

| Symptom | Likely cause |
|---|---|
| QR required repeatedly | Auth session invalid or not persisted |
| `Cannot find module` | Node dependencies missing |
| Browser launch failure | Chromium/Puppeteer environment issue |
| Send returns success but message absent | Wrong chat ID or async delivery gap |
| Bridge process exits immediately | Syntax/runtime error in `bot.js` |

Before changing bridge code, run `node --check Termux-WP/bot.js` if the file exists.

---

## 7. Dependency Failures

Check before installing:

```bash
python3 -c "import requests, bs4, jsonschema; print('python deps OK')"
pkg list-installed | grep -E 'python|nodejs|clang|cmake|rust'
```

If a dependency is missing and setup expects it, install through the established package manager:

- Python packages: `pip install <package>`
- Termux packages: `pkg install <package>`
- Node packages: `npm install` inside the Node project directory

Do not run package removal or broad upgrades as a troubleshooting shortcut.

---

## 8. Finishing a Troubleshooting Task

A troubleshooting task is complete when:

- The root cause is identified, not merely masked.
- The smallest necessary fix is applied.
- Syntax/import/behavior checks pass for the affected path.
- Any temporary diagnostic files are removed or left in `workspace/` with a clear reason.
- The final report includes the symptom, root cause, fix, and proof.

If the root cause cannot be fixed in the current constraints, leave the system in a stable state and report the exact blocker.

