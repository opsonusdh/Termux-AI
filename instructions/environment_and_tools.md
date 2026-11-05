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
| `termux-toast` | Brief on-screen toast message |
| `termux-clipboard-get` / `termux-clipboard-set` | Clipboard access |
| `termux-contact-list` | Contact access (with permission) |
| `termux-sms-list` / `termux-sms-send` | SMS access (with permission) |
| `termux-location` | GPS/network location (with permission) |
| `termux-sensor` | Sensor data (accelerometer, gyroscope, etc.) |

Wrappers: `tools/wrapper_termux_battery_status.py`, `tools/wrapper_termux_wifi_scaninfo.py`, `tools/wrapper_termux_*.py`

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
| `config/api.keys.template` | Placeholder shape of `api.keys` — the file a user copies | ✗ |
| `config/config.json` | Runtime settings (STT path, TTS toggle, `notify`) | ✗ |
| `config/capability_registry.json` | Module/function capability registry | ✗ |
| `config/google_oauth.json` | Google OAuth client id + secret | ✓ |
| `data/google_tokens/` | Per-service OAuth access + refresh tokens | ✓ |
| `data/cli_history` | Prompt history — a pasted credential lands here | ✓ |

Always load via `paths` constants — never hardcode paths.

---

## 7. Package and Dependency Management

1. **Check first.** Run `pip list | grep <package>` or `import <package>` before installing.
2. **Standard library first.** Prefer `json`, `subprocess`, `multiprocessing`, `pathlib`, `sqlite3`, and `threading`.
3. **Required packages** (installed by `setup.sh`): `openai`, `httpx`, `requests`, `beautifulsoup4`, `jsonschema`. `prompt_toolkit` is optional (richer prompt, persistent history); `tiktoken` is **not** used by this project.
4. **Termux system packages** — install via `pkg install <package>`, not `apt`. Never run `pkg remove` or system-level changes without explicit user authorization.
5. **Python packages** — use `pip install`, not `pkg install python-<package>` unless explicitly needed for binary wheels.

---

## 8. Android & Termux Architecture Nuances

- **Filesystem & Sandbox:** Termux runs on an Android kernel (`aarch64`) with Bionic libc. `/tmp` is owned by `shell` (mode 771) and is **not** writable by the Termux app user. Always write temporary files to `workspace/` or project paths.
- **Path Resolution:** Always resolve `workspace` to `~/Termux-AI/workspace`, never `~/workspace`.
- **Clock Drift & Timing:** Android system suspend causes `CLOCK_REALTIME` to drift ahead of monotonic time. Always use `time.monotonic()` for durations, deadlines, and timeout budgets; never use `time.time()`.
- **DNS Resolution in Go Binaries:** Binaries like `cloudflared` cannot resolve DNS on Android because `/etc/resolv.conf` is missing. Use `proot` to bind a writable rootfs with `resolv.conf` and CA certificates. Surface-Tor health checks use socket-level port checks and process polling instead of HTTP 502 status codes.

---

## 9. Shell Runner Mechanics & Safety

- **Stage vs. Chain Timeouts:** In `core/shell_runner.py`, the `timeout` parameter applies *per stage* in a pipeline, while `total_timeout` sets an overall chain ceiling.
- **Process Tree Cleanup:** On timeout expiry, the entire process tree is terminated via `killpg` and `/proc` session inspection to prevent orphaned background processes.
- **Bash DEBUG Trap:** Chain status masking across pipelines (`|`, `||`, `;`) is caught by a bash `DEBUG` trap reading `${PIPESTATUS[0]}` before subsequent commands reset it.
- **Structural Shell Parsing:** `core/permissions.py` uses `core/shell_parse.py` for AST-level command inspection. Never split commands textually at `;`. Note: When `autonomous: true` is configured in `config/config.json`, operations are auto-approved except for protected credentials (`api.keys`, tokens).

---

## 10. Persistent Browser & Web Scraping (CDP)

- **Persistent Chromium Sessions:** `browser(action=...)` maintains live CDP sessions (`b1`, `b2`...) with persistent cookies and state. Viewport is 1024x681 (DPR 1).
- **Coordinate-Free Discovery & Centered Clicks:** Never guess click coordinates. Use `find` to discover DOM elements. Always scroll the target into view (`scrollIntoView({block:'center', behavior:'instant'})`) before computing `getBoundingClientRect()`, and click the center of the bounding box.
- **Screenshot Constraints:** Headless Chrome (`--headless=new`) does not rasterize `data:` URLs (yielding blank white output). Always test against real HTTP(S) URLs. Screenshots are scaled to $\le 1024\text{px}$ JPEG.
- **Hidden Content Stripping:** The fallback scraper (`requests` + `BeautifulSoup`) strips `display:none`, `visibility:hidden`, `[hidden]`, and `<noscript>` elements to avoid leaking invisible page content.
- **Search Engine Captchas:** If DuckDuckGo or Startpage blocks automated requests with captchas, fallback to Bing with selector `#b_results` or interactive browser automation.

---

## 11. Plugins & External Services

- **Plugin Lifecycle:** Managed by `core/plugin_manager.py`. Tools are exposed only when the plugin is enabled and authenticated.
- **Google OAuth Client Type:** Google OAuth for Gmail, Calendar, and Drive requires a **"Desktop app"** client type to permit loopback (127.0.0.1) and PKCE flow. "TV / Limited Input" clients fail with scope and redirect errors.
- **Multimodal Payload Invariants:** Image attachments must be placed in a `user` role turn following the tool output, as Google Gemini rejects image content parts inside `tool` role messages.
- **NVIDIA Image Generation Specs:** Hosted GenAI endpoints (`https://ai.api.nvidia.com/v1/genai/{model}`) require strict adherence to model parameter envelopes (max 1,062,400 pixels total, CFG limits, valid aspect ratio literals). Image models belong exclusively in `IMAGE_MODEL_SLOTS`.
