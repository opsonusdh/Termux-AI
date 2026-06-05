"""orchestration/manager.py — Sequential multi-worker task manager."""
import json
import multiprocessing
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from orchestration.protocol import IPCProtocol
from orchestration.worker import Worker

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"


class Manager:
    def __init__(self):
        self.protocol = IPCProtocol()
        self.tasks    = []
        self.history  = []

    def load_tasks(self, tasks_list: list[dict]) -> None:
        self.tasks = [
            {
                "id":            t.get("id"),
                "worker_name":   t.get("worker_name", f"Worker-{t.get('id')}"),
                "type":          t.get("type", "shell"),
                "command":       t.get("command", ""),
                "mock_response": t.get("mock_response"),
                "status":        "pending",
            }
            for t in tasks_list
        ]

    def _run_worker_process(self, queue, worker_name: str, task: dict) -> None:
        w      = Worker(worker_name)
        result = w.execute_task(task)
        queue.put({"id": task["id"], "result": result})

    def run_all(self) -> dict:
        print(f"{GRAY}[MANAGER] Starting {len(self.tasks)} task(s)...{RESET}")
        for task in self.tasks:
            task_id     = task["id"]
            worker_name = task["worker_name"]
            print(f"{GRAY}[MANAGER] Delegating task {task_id} → {worker_name}{RESET}")
            task["status"] = "running"

            p = multiprocessing.Process(
                target=self._run_worker_process,
                args=(self.protocol.queue, worker_name, task),
            )
            p.start()
            response = self.protocol.receive_status(timeout=30)
            p.join()

            if isinstance(response, dict) and response.get("id") == task_id:
                res           = response["result"]
                task["status"] = res.get("status", "completed")
                task["result"] = res
                self.history.append(res)
                print(f"{GRAY}[MANAGER] Task {task_id} → {task['status']}{RESET}")
            else:
                task["status"] = "failed"
                task["result"] = response
                self.history.append(response)
                print(f"{RED}[MANAGER] Task {task_id} failed to report: {response}{RESET}")

            if task["status"] not in ("success", "completed"):
                print(f"{RED}[MANAGER] Aborting — task {task_id} failed.{RESET}")
                break

        success = all(t["status"] in ("success", "completed") for t in self.tasks)
        return {
            "status":  "success" if success else "failed",
            "tasks":   self.tasks,
            "history": self.history,
        }
