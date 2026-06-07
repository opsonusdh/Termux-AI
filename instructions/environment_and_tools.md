# Environment, Security, and Tool Integration

This document covers safe operation in the Termux sandbox, Termux API usage, the `tools/` wrapper pattern, package management, and efficient tool use.

---

## 1. Operating Safely in Termux

### Sandbox Boundaries
- **Write only inside `~/Termux-AI`** for all file creation, modification, or deletion.
- **No global paths.** Do not assume `/tmp`, `/var`, or `/usr` are writable. Use paths from `paths.py`.
- **Sensitive operations** — commands involving networking changes, package removal, or credential configuration must pass through `permissions.py` (`validate_command()`) before execution.

### IPC Restrictions
- Avoid named pipes (FIFOs) and POSIX shared memory — Android's security sandbox restricts them.
- Use `multiprocessing.Queue` for inter-process communication.
- Use append-only `.jsonl` files for persistent event logs.

### Resource Limits
- Keep background threads lightweight — only the context summariser and reflection logger are permitted daemon threads.
- Always close file handles and join subprocesses. Use `finally` blocks for cleanup.
- If battery is critically low (< 15%, not charging), skip non-essential background work.

---

## 2. Tool Use Efficiency

**The core rule:** Choose the right tool for the operation type. Using the wrong tool wastes round trips, hits rate limits, and introduces inconsistency.

| Operation | Right approach | Wrong approach |
|---|---|---|
| Same change in N places | Script or sed in one pass | N individual `write_file` calls |
| Read a file before editing | `read_file` once | Assume you remember its contents |
| Check if a binary exists | `which <cmd>` | Report it unavailable without checking |
| Verify a change landed | `grep` or `python3` count | Trust that the write succeeded |
| Install a package | Check if already installed first | `pkg install` unconditionally |

### When to Use a Script

Write a Python or bash script whenever:
- The same transformation appears in more than three locations
- The change is mechanically regular (add a line after every matching pattern, rename every occurrence of X, etc.)
- Individual edits would require reading and writing the same file multiple times

A one-pass script is faster, more consistent, and does not consume multiple read/write round trips. After running it, verify with `grep` or `python3` to confirm N changes landed.

### Minimizing Round Trips

- Read a file once and do all your analysis before writing.
- Batch all information-gathering (reads, greps, probes) before any writes.
- If multiple files need the same transformation, process them all in one script rather than sequentially.

---

## 3. Termux API Integration

| Command | Use |
|---|---|
| `termux-battery-status` | Current charge level and charging state |
| `termux-wifi-connectioninfo` | Active Wi-Fi connection details |
| `termux-wifi-scaninfo` | Nearby network scan |
| `termux-vibrate` | Haptic alert on task completion or failure |
| `termux-notification` | System notification |

Wrappers: `tools/wrapper_termux_battery_status.py`, `tools/wrapper_termux_wifi_scaninfo.py`

### Verify Before Reporting Unavailability
Before reporting that a command or API is unavailable, verify:
```bash
which termux-battery-status
pkg search termux-api
```
Claiming something is unavailable without checking is a false claim.

---

## 4. The `tools/` Wrapper Pattern

All Termux API calls must be wrapped in a clean Python class inside `~/Termux-AI/tools/`.

**Important:** `tools/` (Termux API wrappers) and `core/tools.py` (LLM-callable tool implementations) are separate modules. Do not confuse them.

### Standard Wrapper Structure

```python
# tools/wrapper_<capability_name>.py
import json
import subprocess

def get_<capability>() -> dict:
    try:
        result = subprocess.run(
            ["termux-<capability>"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return {"status": "success", "data": json.loads(result.stdout)}
        return {"status": "failed", "error": result.stderr.strip()}
    except Exception as e:
        return {"status": "error", "error_type": type(e).__name__, "message": str(e)}
```

### Placement Rules
- Core wrappers and shared utilities: `tools/tool_wrappers.py`
- Standalone per-capability wrappers: `tools/wrapper_<capability_name>.py`
- Export through `tools/__init__.py` for clean imports

---

## 5. sys.path Convention

Every module that imports across packages must set up `sys.path` the same way — `core/` first, root second:

```python
import os, sys
_CORE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_CORE)
if _CORE not in sys.path: sys.path.insert(0, _CORE)
if _ROOT not in sys.path: sys.path.insert(1, _ROOT)
```

This ensures `import tools` resolves to `core/tools.py`, not to the `tools/` package.

---

## 6. Config and Secrets

| File | Purpose | Gitignored |
|---|---|---|
| `config/api.keys` | LLM provider API keys (JSON) | ✓ |
| `config/config.json` | Runtime settings (STT path, TTS toggle) | ✓ |
| `config/capability_registry.json` | Module/function capability registry | ✗ |

Always load via `paths` constants — never hardcode paths.

---

## 7. Package and Dependency Management

1. **Check first.** Run `pip list | grep <package>` or `import <package>` before installing.
2. **Standard library first.** Prefer `json`, `subprocess`, `multiprocessing`, `pathlib`, `sqlite3`, and `threading`.
3. **Required packages** (installed by `setup.sh`): `openai`, `requests`, `beautifulsoup4`, `jsonschema`
4. **Termux system packages** — install via `pkg install <package>`, not `apt`. Never run `pkg remove` or system-level changes without explicit user authorization.
