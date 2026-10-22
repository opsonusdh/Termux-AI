"""orchestration/worker.py — Task execution worker (shell / python / mock)."""
import json
import os
import subprocess
import sys
import time

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

#: Hard ceiling for a single worker task.  Without this a hung command would
#: block the orchestrator forever.
TASK_TIMEOUT = 300


class Worker:
    def __init__(self, name: str):
        self.name = name

    def execute_task(self, task: dict) -> dict:
        """
        Execute a task dict.
        task keys: type ('shell'|'python'|'mock'), command (str), mock_response (dict).
        Returns a result dict with status, stdout/stderr, duration.
        """
        task_type = task.get('type', 'shell')
        command   = task.get('command', '')
        start     = time.monotonic()

        print(f"{GRAY}[WORKER:{self.name}] type={task_type}{RESET}")

        if task_type == 'shell':
            if sys.platform == "win32":
                return self._run_subprocess(command, 'shell', start, shell=True)
            else:
                return self._run_subprocess(['bash', '-c', command], 'shell', start)

        elif task_type == 'python':
            py_bin = 'python' if sys.platform == "win32" else 'python3'
            if os.path.exists(command):
                cmd = [py_bin, command]
            else:
                cmd = [py_bin, '-c', command]
            return self._run_subprocess(cmd, 'python', start)

        elif task_type == 'mock':
            mock = task.get('mock_response', {"status": "success", "message": "Mock OK"})
            return {
                "worker":    self.name,
                "task_type": "mock",
                "status":    mock.get("status", "success"),
                "result":    mock,
                "duration":  time.monotonic() - start,
            }
        else:
            return {
                "worker":    self.name,
                "status":    "error",
                "error":     f"Unknown task type: {task_type}",
                "duration":  time.monotonic() - start,
            }

    def _run_subprocess(self, cmd, task_type: str, start: float, shell: bool = False) -> dict:
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                shell=shell,
                timeout=TASK_TIMEOUT,
            )
            return {
                "worker":     self.name,
                "task_type":  task_type,
                "status":     "success" if res.returncode == 0 else "failed",
                "returncode": res.returncode,
                "stdout":     res.stdout.strip(),
                "stderr":     res.stderr.strip(),
                "duration":   time.monotonic() - start,
            }
        except subprocess.TimeoutExpired:
            return {
                "worker":    self.name,
                "task_type": task_type,
                "status":    "timeout",
                "error":     f"task exceeded {TASK_TIMEOUT}s",
                "duration":  time.monotonic() - start,
            }
        except Exception as e:
            return {
                "worker":    self.name,
                "task_type": task_type,
                "status":    "error",
                "error":     str(e),
                "duration":  time.monotonic() - start,
            }


if __name__ == "__main__":
    if len(sys.argv) > 1:
        try:
            task_data   = json.loads(sys.argv[1])
            worker_name = task_data.get('worker_name', 'GenericWorker')
            w = Worker(worker_name)
            print(json.dumps(w.execute_task(task_data)))
        except Exception as e:
            print(json.dumps({"status": "error", "error": str(e)}))
