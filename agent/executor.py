"""
agent/executor.py — Thin execution shim for Termux-AI.

Real task execution is done by run_agent_step() in core/llm_client.py.
This module provides the record-and-validate wrapper used by the reflection
and self-correction pipeline.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent import validator
from reflection import ReflectionLoop

GRAY  = "\033[90m"
RESET = "\033[0m"


def execute_plan(plan: dict) -> dict:
    """
    Wrap a plan execution result: validate, record to reflection log, return.

    In agent mode the actual LLM worker runs inside run_agent_step(). This
    function is the post-execution bookkeeping layer called by self_correction.
    """
    print(f"{GRAY}[EXECUTOR] Recording plan: {plan}{RESET}")
    result  = {"result": f"Executed: {plan}"}
    valid   = validator.validate_execution(result)
    result.update(valid)
    success = result.get("validation") == "Success"
    ReflectionLoop.record(plan, result, success)
    return result
