"""orchestration/protocol.py — Multiprocessing queue IPC wrapper."""
import multiprocessing

GRAY  = "\033[90m"
RESET = "\033[0m"


class IPCProtocol:
    def __init__(self):
        self.queue = multiprocessing.Queue()

    def send_status(self, data: dict) -> None:
        self.queue.put(data)

    def receive_status(self, timeout: int = 5) -> dict:
        try:
            return self.queue.get(timeout=timeout)
        except Exception as e:
            return {"status": "error", "message": f"Timeout or IPC error: {e}"}
