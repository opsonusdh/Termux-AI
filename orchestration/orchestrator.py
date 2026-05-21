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

class Orchestrator:
    def __init__(self):
        self.workspace = paths.ORCHESTRATION_DIR
        self.protocol = IPCProtocol()
        
    def delegate(self, worker_script, task_data):
        """Delegates a task to a worker script and waits for status via IPC."""
        # Note: We need to pass the queue to the worker for this to work properly.
        # However, for now, we will use the orchestrator's queue instance.
        cmd = ["python3", os.path.join(self.workspace, worker_script), json.dumps(task_data)]
        
        # Start the worker process
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        # Wait for status from the protocol queue
        status = self.protocol.receive_status(timeout=10)
        
        process.wait()
        return status

if __name__ == "__main__":
    print("Orchestrator updated to use Queue IPC.")
