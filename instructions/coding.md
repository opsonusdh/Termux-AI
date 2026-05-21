# Coding Instructions & Standards

This document establishes the official coding standards, architectural principles, and safety guidelines for software engineering within the Orion architecture operating under the Termux environment on Android.

---

## 1. Environment & Architecture Constraints

Termux is a sandboxed Linux-like environment running inside Android. It has unique constraints that differ from standard server environments:

- **Strict Sandbox Permissions:** Do not assume global file paths (like `/tmp`, `/var`, or `/usr`) are writable or accessible. Always use user-space paths under `~/ai_root` or locate system files relative to the Termux home prefix (`/data/data/com.termux/files/home`).
- **Path Isolation:** All files must resolve their paths through the centralized `paths.py` module. Never hard-code string paths or rely on relative imports that assume a specific working directory.
- **IPC Restrictions:** Avoid standard Linux named pipes (FIFOs) or shared memory segments in `/tmp` because Android's security sandbox restricts permissions. For inter-process communication, prefer `multiprocessing.Queue`, JSON-lines (`.jsonl`) event logs, or SQLite databases.
- **Resource Limitations:** Mobile CPUs regulate processes aggressively. Always manage threads, sub-processes, and memory carefully. Ensure processes terminate gracefully, close file handles, and avoid high-duty busy-wait loops.

---

## 2. Code Organization & Modular Design

To prevent codebase degradation, all code must adhere to clean architectural separation:

- **Core Module (`/core/`):** Contains the foundational engine, prompt structures, direct LLM communication clients, permission managers, and main UI renderers. Modifying core files requires high precision and comprehensive regression testing.
- **Orchestration (`/orchestration/`):** Houses the `Manager`, `Worker`, and `IPCProtocol` modules. This is the multi-agent task runner. Scripts here must be non-blocking and strictly adhere to the queue communication contract.
- **Tools (`/tools/`):** Contains discrete, stateless wrapper scripts for external API integration or local device capabilities (e.g., Termux API, network scans). These wrappers must be self-contained and importable.
- **Reflection (`/reflection/`):** Holds diagnostic modules (`reflector.py`, `self_correction.py`) used to analyze failures and propose run-time code modifications or alternative execution paths.
- **Data & Configuration (`/data/`):** Stores schemas, JSON capabilities, and system states. Keep logic completely separated from data storage.

---

## 3. Robust Error Handling & Logging

All code must implement resilient error-handling mechanics:

- **Never Swallow Errors Silently:** Avoid empty `except:` blocks or `except Exception: pass`. Always capture, log, and propagate or gracefully handle the exception.
- **Structured JSON Logging:** Use the centralized logging system under `~/ai_root/logs/`. Save errors with precise metadata (timestamp, file, function, exception type, message, traceback) to allow `reflector.py` to diagnose issues programmatically.
- **Graceful Fail-safes:** Provide logical fallbacks. If a hardware-level sensor check fails (e.g., battery status or Wi-Fi scan), return a clean `{"status": "error", "error_type": "hardware_unavailable", "message": "..."}` rather than allowing the application to crash.

---

## 4. Multi-Process & Threading Principles

When writing concurrent or delegated tasks:

- **Process Isolation:** Workers must be spawned as separate processes (`multiprocessing.Process`) to prevent GIL contention and ensure that memory leaks or crashes in a worker script do not bring down the main agent coordinator.
- **Queue Synchronization:** Use queue-based IPC. When a worker finishes or encounters an error, it must report its state via the synchronized `multiprocessing.Queue` and terminate.
- **Clean Termination Contract:** 
  - The Manager process must always call `join()` on spawned worker processes.
  - Implement thread/process timeouts to prevent hung processes from blocking execution indefinitely.
  - Always clean up orphaned resources (such as temporary files or open sockets) in a `finally` block.

---

## 5. Centralized Path Resolution (`paths.py`)

All file reads, writes, and execution pathways must import `paths` and reference files dynamically.

### Example Configuration (`paths.py` Usage):
```python
import sys
from paths import DATA_DIR, TOOLS_DIR, ORCHESTRATION_DIR

# Correct way to resolve a state file path:
state_file_path = DATA_DIR / "state.json"

# Correct way to reference a tool script:
battery_tool_script = TOOLS_DIR / "wrapper_termux_battery_status.py"
```

---

## 6. Testing & Self-Verification (TDD)

All additions or refactors must be validated with an end-to-end verification script before concluding a task:

1. **Unit Tests:** Write tiny, modular tests for discrete logic (e.g., JSON parsers, path utilities).
2. **Integration Tests:** Verify that cross-module dependencies (e.g., `Manager` delegating to `Worker` using `IPCProtocol`) work in concert under Termux environment constraints.
3. **Execution Verification:** Run tests using python interpreter directly and check standard exit codes (`sys.exit(0)` for success). Inspect outputs and save logs for proof.
