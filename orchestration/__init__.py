"""
orchestration — Multi-process task delegation for Termux-AI.

Public API:
  Manager().run_all()                                → sequential task execution
  Worker(name).execute_task(task)                    → shell/python/mock execution
  IPCProtocol()                                      → multiprocessing.Queue wrapper
"""
from orchestration.manager import Manager
from orchestration.worker import Worker
from orchestration.protocol import IPCProtocol

__all__ = ["Manager", "Worker", "IPCProtocol"]
