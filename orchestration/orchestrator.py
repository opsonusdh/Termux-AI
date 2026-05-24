import json
import subprocess
import os
import sys
import multiprocessing

# Add project root to sys.path
orchestration_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(orchestration_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

import paths
from orchestration.protocol import IPCProtocol

# Gray debug trail colors
GRAY  = "\033[90m"
RESET = "\033[0m"

class Orchestrator:
    def __init__(self):
        self.workspace = paths.ORCHESTRATION_DIR
        self.protocol = IPCProtocol()
        
    def delegate(self, worker_script, task_data):
        """Delegates a task to a worker script and waits for status via IPC."""
        # Note: We need to pass the queue to the worker for this to work properly.
        # However, for now, we will use the orchestrator's queue instance.
        script_path = os.path.join(self.workspace, worker_script)
        cmd = ["python3", script_path, json.dumps(task_data)]
        
        cmd_str = " ".join(cmd)
        print(f"{GRAY}[ORCHESTRATOR] Delegating task to {worker_script}{RESET}")
        print(f"{GRAY}[ORCHESTRATOR] Command: {cmd_str}{RESET}")
        
        # Start the worker process
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        # Wait for status from the protocol queue
        print(f"{GRAY}[ORCHESTRATOR] Waiting for protocol status...{RESET}")
        status = self.protocol.receive_status(timeout=10)
        
        process.wait()
        
        if process.returncode != 0:
            print(f"{GRAY}[ORCHESTRATOR] Worker failed (code: {process.returncode}). StdErr: {process.stderr.read()}{RESET}")
        else:
            print(f"{GRAY}[ORCHESTRATOR] Worker completed successfully.{RESET}")
            
        print(f"{GRAY}[ORCHESTRATOR] Received status: {status}{RESET}")
        return status

if __name__ == "__main__":
    print(f"{GRAY}[ORCHESTRATOR] Orchestrator initialized.{RESET}")
