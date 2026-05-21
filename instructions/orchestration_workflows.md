# Agentic Orchestration & Collaboration Workflows

This document details the architectural blueprints and operational patterns of the Orion multi-agent orchestration system. It guides the creation, execution, and monitoring of specialized "Worker" subprocesses by the central "Manager" coordinating agent.

---

## 1. Architectural Topology

Orion separates task management (high-level orchestration) from task execution (low-level script running) using an actor-like process-isolation model:

```
                  ┌──────────────────────┐
                  │  Manager (Primary)   │
                  └──────────┬───────────┘
                             │ Spawns
            ┌────────────────┼────────────────┐
            ▼                ▼                ▼
     ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
     │  Worker A   │  │  Worker B   │  │  Worker C   │
     │   (Shell)   │  │  (Python)   │  │   (Mock)    │
     └──────┬──────┘  └──────┬──────┘  └──────┬──────┘
            │                │                │
            └───────────┬────┴────────────────┘
                        ▼ Writes to
             ┌─────────────────────┐
             │ multiprocessing.Queue│ (non-blocking)
             └──────────┬──────────┘
                        │ Reads from
                        ▼
                  ┌───────────┐
                  │  Manager  │
                  └───────────┘
```

- **Manager (`manager.py` / `orchestrator.py`):** Holds the high-level goal, schedules sequence of execution, handles runtime dependency injection, and acts on feedback/errors.
- **Worker (`worker.py`):** Isolated, sandboxed processes executing specific commands or scripts.
- **IPCProtocol (`protocol.py`):** Standardized, synchronized communication pipeline implemented using Python's robust `multiprocessing.Queue` to avoid environment deadlocks common with FIFO pipes in Termux.

---

## 2. Worker Abstraction

Every Worker script or class must inherit from or conform to the standard `Worker` structure in `~/ai_root/orchestration/worker.py`.

### Task Input Configuration:
A task delegated to a worker is structured as a JSON-compatible dictionary:
```json
{
  "task_id": "unique_task_01",
  "name": "SystemStatusCheck",
  "type": "shell",
  "command": "termux-battery-status"
}
```
*Supported Types:*
1. `shell`: Direct shell executions (bash commands).
2. `python`: Inline Python snippets or targeted python script execution.
3. `mock`: Simulation mode for dry-run testing.

### Standardized Execution Report (Output):
Every worker must transmit its status back through the queue using a strictly formatted output schema:
```json
{
  "task_id": "unique_task_01",
  "status": "success",
  "exit_code": 0,
  "stdout": "{\n  \"health\": \"good\",\n  \"percentage\": 84\n}",
  "stderr": "",
  "duration": 0.245
}
```
*Valid Statuses:*
- `success`: Task executed completely with zero exit code.
- `failed`: Task executed but returned a non-zero exit code.
- `error`: Uncaught Python exception, timeout, or system failure occurred preventing execution.

---

## 3. Communication Protocol & Lifecycle

To ensure system stability, process creation and cleanup must follow a strict lifecycle protocol:

### Sequence of Delegation & Cleanup:
1. **Queue Initialization:** The Manager instantiates a shared `multiprocessing.Queue()`.
2. **Worker Preparation:** The Manager maps a subtask to a task definition, instantiating the `Worker` with the necessary parameters and injecting the communication queue reference.
3. **Process Spawn:** The Manager instantiates a `multiprocessing.Process(target=worker.run)` and calls `process.start()`.
4. **Non-Blocking Listen:** The Manager listens for the status report using `queue.get(timeout=T)` where `T` is the task-specific timeout limit.
5. **Process Join:** Once the report is received (or a timeout exception occurs), the Manager calls `process.join(timeout=5)`.
6. **Force Cleanup:** If the process is still alive after the join timeout, the Manager calls `process.terminate()` followed by `process.close()`.
7. **Queue Management:** The Queue should remain open across tasks but must be properly drained and closed during system shutdown.

---

## 4. Expanding Worker Capabilities

To register a new capability or worker type within Orion:

1. **Register in `capability_registry.json`:** Add the schema, constraints, and operational metadata.
2. **Update `worker.py` Execute Logic:** If a new execution engine is required (e.g., SQLite execution, direct API connection), implement the logic as a discrete method inside the `Worker` class.
3. **Verify via Integration Tests:** Run the E2E verification suite (`test_orchestration.py`) to confirm that task delegation, IPC reporting, and process teardown are completely functional.
