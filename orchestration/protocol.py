import multiprocessing

class IPCProtocol:
    def __init__(self):
        self.queue = multiprocessing.Queue()

    def send_status(self, data):
        """Sends a status update via the queue."""
        self.queue.put(data)

    def receive_status(self, timeout=5):
        """Receives a status update from the queue with a timeout."""
        try:
            return self.queue.get(timeout=timeout)
        except Exception as e:
            return {"status": "error", "message": f"Timeout or error: {str(e)}"}

if __name__ == "__main__":
    # Minimal test
    proto = IPCProtocol()
    proto.send_status({"task": "test", "result": "success"})
    print(f"Received: {proto.receive_status()}")
