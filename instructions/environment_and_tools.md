# Environment, Security, and Tool Integration Guidelines

This document provides instructions on how to interact with the Termux shell environment, access native Android capabilities safely, execute system commands, and manage localized utility tools within the Orion architecture.

---

## 1. Operating Safely in Termux

Operating inside Termux on a personal Android device requires maintaining a strict balance between high autonomy and device safety.

### Permissions & Context Limits:
- **Sandbox Boundary:** All active modifications, creations, or deletions must be strictly confined to `~/ai_root`. 
- **Outside `~/ai_root` access:** Read-only access is permitted anywhere standard UNIX permissions allow (such as reading shared storage or inspecting logs). Never write, alter, or delete any files outside `~/ai_root` without explicit per-action authorization from the user.
- **Sensitive Commands:** Commands involving device modification, networking adjustments, package removals, or credential configurations must be verified for safety before execution.

---

## 2. Termux API Integration

Termux provides extensive bridges to Android hardware and OS-level telemetry. Utilize these capabilities to contextualize actions:

- **Battery Status (`termux-battery-status`):** Monitor device power levels. Do not run heavy processing tasks, complex model training, or high-concurrency loops if battery levels are critically low (< 15%) and not charging.
- **Wi-Fi Details (`termux-wifi-connectioninfo`, `termux-wifi-scaninfo`):** Used to verify connectivity state. Handle failures gracefully if Wi-Fi or GPS services are disabled on the device.
- **Vibrate/Notifications (`termux-vibrate`, `termux-notification`):** Can be used to signal the user upon completion of long-running operations or during system failures.
- **Speech-to-Text (STT) & Telemetry:** Utilize the local whisper integration under `~/ai_root/Termux-STT/` for parsing spoken voice prompts.

### Device Capability Discovery:
Never assume a command or API is missing. Before reporting that a feature is unavailable, perform programmatic discovery:
1. Check if the binary exists using `which <command>` or `type <command>`.
2. Inspect if the package is available in repositories via `pkg search <package>`.
3. Test permission states using local test script runs.

---

## 3. Tool Architecture & Wrapper Pattern

All tool integration must reside inside `~/ai_root/tools/`. Do not write loose script files in the system. Use the standardized Wrapper Pattern.

### The Wrapper Pattern:
Every system command or external API must be wrapped in a clean Python class. This guarantees standardized schema processing and type-safety:

```python
# Standard Tool Wrapper Schema example
import json
import subprocess
from paths import LOGS_DIR

class TermuxToolWrapper:
    def __init__(self):
        pass

    def run_command(self, cmd_args):
        try:
            result = subprocess.run(cmd_args, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return {"status": "success", "data": json.loads(result.stdout)}
            else:
                return {"status": "failed", "error": result.stderr}
        except Exception as e:
            return {"status": "error", "error_type": type(e).__name__, "message": str(e)}
```

### Directory Placement:
- Core wrappers: `~/ai_root/tools/tool_wrappers.py`
- Specialized standalone wrappers: `~/ai_root/tools/wrapper_<capability_name>.py`
- Expose the capabilities: Expose through `~/ai_root/tools/__init__.py` so they can be imported cleanly across the architecture.

---

## 4. Package & Dependency Management

When code requires external third-party Python packages or Termux packages:
1. **Detect Availability:** Check if the package is already installed (`import pkg_resources` or `pip list`).
2. **Local Scope Isolation:** If installing python packages, make sure they do not break existing modules. Use standard library modules (`sqlite3`, `json`, `multiprocessing`, `subprocess`, `pathlib`) as much as possible to avoid dependency bloat.
3. **User-Friendly Installation:** If an external system binary is required (e.g., `jq`, `curl`, `ffmpeg`), notify the user or run safe programmatic `pkg install` commands inside isolated workers only when absolutely necessary and safe.
