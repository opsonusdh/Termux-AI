"""orchestration/worker.py — Task execution worker (shell / python / mock)."""
import json
import os
import subprocess
import sys
import time

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"


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
        start     = time.time()

        print(f"{GRAY}[WORKER:{self.name}] type={task_type}{RESET}")

        if task_type == 'shell':
            return self._run_subprocess(['bash', '-c', command], 'shell', start)

        elif task_type == 'python':
            if os.path.exists(command):
                cmd = ['python3', command]
            else:
                cmd = ['python3', '-c', command]
            return self._run_subprocess(cmd, 'python', start)

        elif task_type == 'mock':
            mock = task.get('mock_response', {"status": "success", "message": "Mock OK"})
            return {
                "worker":    self.name,
                "task_type": "mock",
                "status":    mock.get("status", "success"),
                "result":    mock,
                "duration":  time.time() - start,
            }
        else:
            return {
                "worker":    self.name,
                "status":    "error",
                "error":     f"Unknown task type: {task_type}",
                "duration":  time.time() - start,
            }

    def _run_subprocess(self, cmd: list, task_type: str, start: float) -> dict:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
            return {
                "worker":     self.name,
                "task_type":  task_type,
                "status":     "success" if res.returncode == 0 else "failed",
                "returncode": res.returncode,
                "stdout":     res.stdout.strip(),
                "stderr":     res.stderr.strip(),
                "duration":   time.time() - start,
            }
        except Exception as e:
            return {
                "worker":    self.name,
                "task_type": task_type,
                "status":    "error",
                "error":     str(e),
                "duration":  time.time() - start,
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
