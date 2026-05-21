"""Self‑Correction Module (prototype)

This module provides a minimal example of how Orion could attempt to correct a
failed execution. It inspects the latest entry in the reflection log (produced by
``reflection.py``) and, if the ``validation`` field indicates ``Failure``, it
re‑executes the original plan using the ``executor`` module. The result of the
re‑execution is recorded back into the reflection log.

The implementation is deliberately simple – it serves as the proof‑of‑concept
required by the "Implement self‑correction mechanisms" subtask.
"""

import os
import json
import sys
from importlib import import_module

# Ensure ai_root is in sys.path
ai_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ai_root not in sys.path:
    sys.path.append(ai_root)

import paths

REFLECTION_LOG = paths.REFLECTION_LOG_FILE


def _load_latest_reflection():
    """Return the most recent reflection entry or ``None``.

    The log is a JSON‑Lines file; we read the last line efficiently.
    """
    try:
        with open(REFLECTION_LOG, 'rb') as f:
            f.seek(-2, os.SEEK_END)
            while f.read(1) != b"\n":
                f.seek(-2, os.SEEK_CUR)
            last = f.readline().decode()
        return json.loads(last)
    except Exception:
        return None


def attempt_correction():
    """Inspect the latest reflection entry and retry on failure.

    Returns a dictionary with keys ``attempted`` (bool) and ``result`` (the
    outcome of the retry or ``None`` if no correction was needed).
    """
    entry = _load_latest_reflection()
    if not entry:
        return {"attempted": False, "result": None, "reason": "no reflection log"}

    # Expected format from ``validator.validate_execution``
    validation = entry.get('result', {}).get('validation')
    if validation != 'Failure':
        return {"attempted": False, "result": None, "reason": "validation succeeded"}

    # Re‑execute the original plan
    plan = entry.get('plan')
    if not plan:
        return {"attempted": False, "result": None, "reason": "no plan stored"}

    # Dynamically import executor
    executor = import_module('executor')
    new_result = executor.execute_plan(plan)

    # Record the correction attempt using the reflection module
    reflection = import_module('reflection')
    reflection.ReflectionLoop.record(plan, new_result, success=(new_result.get('validation') == 'Success'))

    return {"attempted": True, "result": new_result}
