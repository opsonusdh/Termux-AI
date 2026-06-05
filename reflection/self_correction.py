"""
reflection/self_correction.py — Re-executes failed plans using the executor.

Inspects the latest reflection log entry; if validation shows Failure,
re-runs the plan via agent.executor and records the new result.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

GRAY  = "\033[90m"
RESET = "\033[0m"


def attempt_correction() -> dict:
    """
    Inspect the latest reflection entry and retry if it failed.

    Returns {'attempted': bool, 'result': dict | None, 'reason': str}.
    """
    from reflection import ReflectionLoop
    entry = ReflectionLoop.latest_entry()

    if not entry:
        return {"attempted": False, "result": None, "reason": "no reflection log"}

    validation = entry.get('result', {}).get('validation')
    if validation != 'Failure':
        return {"attempted": False, "result": None, "reason": "validation succeeded"}

    plan = entry.get('plan')
    if not plan:
        return {"attempted": False, "result": None, "reason": "no plan stored"}

    print(f"{GRAY}[SELF-CORRECTION] Retrying failed plan: {str(plan)[:60]}{RESET}")

    from agent.executor import execute_plan
    new_result = execute_plan(plan)

    return {"attempted": True, "result": new_result}
