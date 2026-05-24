import multiprocessing

# Gray debug trail colors
GRAY  = "\033[90m"
RESET = "\033[0m"

class IPCProtocol:
    def __init__(self):
        self.queue = multiprocessing.Queue()
        print(f"{GRAY}[PROTOCOL] IPC Queue initialized.{RESET}")

    def send_status(self, data):
        """Sends a status update via the queue."""
        print(f"{GRAY}[PROTOCOL] Sending status: {data}{RESET}")
        self.queue.put(data)

    def receive_status(self, timeout=5):
        """Receives a status update from the queue with a timeout."""
        try:
            status = self.queue.get(timeout=timeout)
            print(f"{GRAY}[PROTOCOL] Received status: {status}{RESET}")
            return status
        except Exception as e:
            print(f"{GRAY}[PROTOCOL] Error receiving status: {str(e)}{RESET}")
            return {"status": "error", "message": f"Timeout or error: {str(e)}"}

if __name__ == "__main__":
    # Minimal test
    proto = IPCProtocol()
    proto.send_status({"task": "test", "result": "success"})
    print(f"{GRAY}[PROTOCOL] Received: {proto.receive_status()}{RESET}")
