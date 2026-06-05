# Environment, Security, and Tool Integration

This document covers safe operation in the Termux sandbox, Termux API usage, the `tools/` wrapper pattern, and package management.

---

## 1. Operating Safely in Termux

### Sandbox Boundaries
- **Write only inside `~/Termux-AI`** for all file creation, modification, or deletion. Read-only access outside this boundary is permitted where standard UNIX permissions allow.
- **No global paths.** Do not assume `/tmp`, `/var`, or `/usr` are writable. Use paths from `paths.py` — all of which resolve under `~/Termux-AI`.
- **Sensitive operations** — commands involving networking changes, package removal, or credential configuration must pass through `permissions.py` (`validate_command()`) before execution.

### IPC Restrictions
- Avoid named pipes (FIFOs) and POSIX shared memory — Android's security sandbox restricts them.
- Use `multiprocessing.Queue` for inter-process communication (see `orchestration/protocol.py`).
- Use append-only `.jsonl` files for persistent event logs (see `logs/reflection.jsonl`, `logs/chunks.jsonl`).

### Resource Limits
- Mobile CPUs throttle aggressively. Keep background threads lightweight — the context summariser and reflection logger are the only permitted daemon threads.
- Always close file handles and join subprocesses. Use `finally` blocks for cleanup.
- If battery is critically low (< 15%, not charging), skip non-essential background work.

---

## 2. Termux API Integration

| Command | Use |
|---|---|
| `termux-battery-status` | Current charge level and charging state |
| `termux-wifi-connectioninfo` | Active Wi-Fi connection details |
| `termux-wifi-scaninfo` | Nearby network scan |
| `termux-vibrate` | Haptic alert on task completion or failure |
| `termux-notification` | System notification |

Wrappers for battery and Wi-Fi are in `tools/`:
- `tools/wrapper_termux_battery_status.py`
- `tools/wrapper_termux_wifi_scaninfo.py`

These are also callable from `agent/state_manager.py` via `get_battery_status()` and `get_wifi_scan_info()`.

### Discovery Before Reporting Unavailability
Before reporting that a command or API is unavailable, verify programmatically:
```bash
which termux-battery-status          # check binary exists
pkg search termux-api                # check if package is available
```

---

## 3. The `tools/` Wrapper Pattern

All Termux API calls must be wrapped in a clean Python class inside `~/Termux-AI/tools/`. Do not write loose scripts elsewhere.

**Note:** `tools/` (Termux API wrappers) and `core/tools.py` (LLM-callable tool implementations) are separate. Do not confuse them.

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

### Adding a New LLM-Callable Tool

1. Write the implementation function in `core/tools.py`.
2. Add the JSON schema to `TOOLS_DESCRIPTION` in `core/llm_client.py`.
3. Add the dispatch case to `_dispatch_tool()` in `core/llm_client.py`.
4. If it wraps a Termux API, create the wrapper in `tools/` first and call it from `core/tools.py`.

---

## 4. sys.path Convention

Every module that needs to import across packages must set up `sys.path` the same way — `core/` first, root second:

```python
import os, sys
_CORE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_CORE)
if _CORE not in sys.path: sys.path.insert(0, _CORE)
if _ROOT not in sys.path: sys.path.insert(1, _ROOT)
```

This ensures:
- `import tools` → `core/tools.py` (not `tools/` package)
- `import paths` → `paths.py` at project root
- `from agent import state_manager` → `agent/state_manager.py`
- `from orchestration import Manager` → `orchestration/__init__.py`

---

## 5. Config and Secrets

| File | Purpose | Gitignored |
|---|---|---|
| `config/api.keys` | LLM provider API keys (JSON) | ✓ |
| `config/config.json` | Runtime settings (STT path, TTS toggle) | ✓ |
| `config/capability_registry.json` | Module/function capability registry | ✗ |

Always load via `paths` constants — never hardcode:

```python
import paths

api_keys_path = paths.API_KEYS_FILE    # config/api.keys
config_path   = paths.CONFIG_FILE      # config/config.json
registry_path = paths.CAPABILITY_REGISTRY  # config/capability_registry.json
```

---

## 6. Package and Dependency Management

1. **Check first.** Run `pip list | grep <package>` or `import <package>` before installing.
2. **Standard library first.** Prefer `json`, `subprocess`, `multiprocessing`, `pathlib`, `sqlite3`, and `threading` to avoid dependency bloat.
3. **Required packages** — installed by `setup.sh`:
   - `openai`, `requests`, `beautifulsoup4`, `jsonschema`
4. **Optional packages** — `edge-tts` (voice TTS), `mpv` (audio playback).
5. **Termux system packages** — install via `pkg install <package>`, not `apt`. Never run `pkg remove` or system-level changes without explicit user authorization.
