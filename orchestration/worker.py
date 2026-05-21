import sys
import json
import subprocess
import os
import time

class Worker:
    def __init__(self, name):
        self.name = name

    def execute_task(self, task):
        """
        task: dict with keys:
          - 'type': 'shell' | 'python' | 'mock'
          - 'command': str (for shell/python)
          - 'mock_response': dict (for mock, optional)
        """
        task_type = task.get('type', 'shell')
        command = task.get('command', '')
        
        start_time = time.time()
        
        if task_type == 'shell':
            try:
                res = subprocess.run(command, shell=True, capture_output=True, text=True)
                duration = time.time() - start_time
                return {
                    "worker": self.name,
                    "task_type": "shell",
                    "status": "success" if res.returncode == 0 else "failed",
                    "returncode": res.returncode,
                    "stdout": res.stdout.strip(),
                    "stderr": res.stderr.strip(),
                    "duration": duration
                }
            except Exception as e:
                duration = time.time() - start_time
                return {
                    "worker": self.name,
                    "task_type": "shell",
                    "status": "error",
                    "error": str(e),
                    "duration": duration
                }
                
        elif task_type == 'python':
            try:
                if os.path.exists(command):
                    cmd = ["python3", command]
                    res = subprocess.run(cmd, capture_output=True, text=True)
                else:
                    cmd = ["python3", "-c", command]
                    res = subprocess.run(cmd, capture_output=True, text=True)
                
                duration = time.time() - start_time
                return {
                    "worker": self.name,
                    "task_type": "python",
                    "status": "success" if res.returncode == 0 else "failed",
                    "returncode": res.returncode,
                    "stdout": res.stdout.strip(),
                    "stderr": res.stderr.strip(),
                    "duration": duration
                }
            except Exception as e:
                duration = time.time() - start_time
                return {
                    "worker": self.name,
                    "task_type": "python",
                    "status": "error",
                    "error": str(e),
                    "duration": duration
                }
                
        elif task_type == 'mock':
            duration = time.time() - start_time
            mock_res = task.get('mock_response', {"status": "success", "message": "Mock execution successful"})
            return {
                "worker": self.name,
                "task_type": "mock",
                "status": mock_res.get("status", "success"),
                "result": mock_res,
                "duration": duration
            }
        else:
            duration = time.time() - start_time
            return {
                "worker": self.name,
                "status": "error",
                "error": f"Unknown task type: {task_type}",
                "duration": duration
            }

if __name__ == "__main__":
    if len(sys.argv) > 1:
        try:
            task_data = json.loads(sys.argv[1])
            worker_name = task_data.get('worker_name', 'GenericWorker')
            worker = Worker(worker_name)
            result = worker.execute_task(task_data)
            print(json.dumps(result))
        except Exception as e:
            print(json.dumps({"status": "error", "error": f"Failed to run worker: {str(e)}"}))
