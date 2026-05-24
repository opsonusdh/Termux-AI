import sys
import os
import json
import time
import multiprocessing

# Add current directory to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from protocol import IPCProtocol
from worker import Worker

# Gray debug trail colors
GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

class Manager:
    def __init__(self):
        self.protocol = IPCProtocol()
        self.tasks = []
        self.history = []

    def load_tasks(self, tasks_list):
        """
        Loads a list of tasks.
        tasks_list: list of dicts, e.g.:
          [
            {"id": 1, "worker_name": "W1", "type": "shell", "command": "echo 'Hello'"},
            {"id": 2, "worker_name": "W2", "type": "mock", "mock_response": {"status": "success"}}
          ]
        """
        self.tasks = []
        for t in tasks_list:
            self.tasks.append({
                "id": t.get("id"),
                "worker_name": t.get("worker_name", f"Worker-{t.get('id')}"),
                "type": t.get("type", "shell"),
                "command": t.get("command", ""),
                "mock_response": t.get("mock_response", None),
                "status": "pending"
            })

    def run_worker_process(self, queue, worker_name, task):
        """Target function for worker process."""
        worker = Worker(worker_name)
        result = worker.execute_task(task)
        queue.put({"id": task["id"], "result": result})

    def run_all(self):
        """Executes all tasks sequentially and tracks status."""
        print(f"{GRAY}[MANAGER] Starting orchestration of {len(self.tasks)} tasks...{RESET}")
        for task in self.tasks:
            task_id = task["id"]
            worker_name = task["worker_name"]
            print(f"{GRAY}[MANAGER] Delegating task {task_id} to {worker_name}...{RESET}")
            
            task["status"] = "running"
            
            # Start worker in a separate process
            p = multiprocessing.Process(
                target=self.run_worker_process, 
                args=(self.protocol.queue, worker_name, task)
            )
            p.start()
            
            # Receive status via IPCProtocol with timeout (non-blocking, robust)
            response = self.protocol.receive_status(timeout=10)
            
            # Join the process to ensure clean termination
            p.join()
            
            if isinstance(response, dict) and "id" in response and response["id"] == task_id:
                res = response["result"]
                task["status"] = res.get("status", "completed")
                task["result"] = res
                self.history.append(res)
                print(f"{GRAY}[MANAGER] Task {task_id} finished with status: {task['status']}{RESET}")
            else:
                # Handle unexpected response or timeout
                task["status"] = "failed"
                task["result"] = response
                self.history.append(response)
                print(f"{RED}[MANAGER] Task {task_id} failed to report properly: {response}{RESET}")
                
            if task["status"] not in ["success", "completed"]:
                print(f"{RED}[MANAGER] Aborting due to failure in task {task_id}.{RESET}")
                break
                
        # Aggregate final status
        success = all(t["status"] in ["success", "completed"] for t in self.tasks)
        return {
            "status": "success" if success else "failed",
            "tasks": self.tasks,
            "history": self.history
        }

if __name__ == "__main__":
    manager = Manager()
    demo_tasks = [
        {"id": 1, "worker_name": "W1", "type": "shell", "command": "echo 'Orchestration step 1'"},
        {"id": 2, "worker_name": "W2", "type": "python", "command": "print('Orchestration step 2')"},
        {"id": 3, "worker_name": "W3", "type": "mock", "mock_response": {"status": "success", "message": "Done!"}}
    ]
    manager.load_tasks(demo_tasks)
    summary = manager.run_all()
    print(f"\n{GRAY}--- Summary ---{RESET}")
    print(json.dumps(summary, indent=2))
