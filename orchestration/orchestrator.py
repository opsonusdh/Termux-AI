"""orchestration/orchestrator.py — High-level subprocess delegator."""
import json
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import paths
from orchestration.protocol import IPCProtocol

GRAY  = "\033[90m"
RESET = "\033[0m"


class Orchestrator:
    def __init__(self):
        self.workspace = paths.ORCHESTRATION_DIR
        self.protocol  = IPCProtocol()

    def delegate(self, worker_script: str, task_data: dict) -> dict:
        """
        Spawn worker_script as a subprocess, pass task_data as JSON arg,
        wait for IPC status, return status dict.
        """
        script_path = os.path.join(self.workspace, worker_script)
        cmd         = ["python3", script_path, json.dumps(task_data)]
        print(f"{GRAY}[ORCHESTRATOR] Delegating → {worker_script}{RESET}")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            return {"status": "error", "message": "Timeout waiting for worker subprocess"}

        if process.returncode != 0:
            print(f"{GRAY}[ORCHESTRATOR] Worker error (code {process.returncode}): {stderr.strip()}{RESET}")
            return {"status": "error", "message": f"Worker failed with code {process.returncode}: {stderr.strip()}"}
        
        print(f"{GRAY}[ORCHESTRATOR] Worker completed.{RESET}")
        try:
            json_line = ""
            for line in reversed(stdout.splitlines()):
                line_stripped = line.strip()
                if line_stripped.startswith("{") and line_stripped.endswith("}"):
                    json_line = line_stripped
                    break
            if not json_line:
                for line in reversed(stdout.splitlines()):
                    if line.strip():
                        json_line = line.strip()
                        break
            return json.loads(json_line)
        except Exception as e:
            return {"status": "error", "message": f"Failed to parse worker stdout: {e}", "stdout": stdout}
