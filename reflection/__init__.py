"""
reflection — Loop recording, analysis, and self-correction for Termux-AI.

Public API:
  ReflectionLoop.record(plan, result, success)  → records to reflection.jsonl
  ReflectionLoop.latest_entry()                 → last log entry or None
  ReflectionLoop.analyze()                       → calls Reflector on latest entry
  attempt_correction()                           → re-runs failed plans
"""
import json
import os
import sys
import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import paths

GRAY  = "\033[90m"
RESET = "\033[0m"


class ReflectionLoop:
    """Records plan→result pairs and surfaces them for analysis."""

    LOG_PATH = paths.REFLECTION_LOG_FILE

    @staticmethod
    def record(plan, result, success: bool) -> dict:
        entry = {
            "timestamp": datetime.datetime.utcnow().isoformat() + 'Z',
            "plan":      plan,
            "result":    result,
            "success":   success,
        }
        os.makedirs(os.path.dirname(ReflectionLoop.LOG_PATH), exist_ok=True)
        with open(ReflectionLoop.LOG_PATH, 'a') as f:
            f.write(json.dumps(entry) + "\n")
        print(f"{GRAY}[REFLECTION] Recorded {'✓' if success else '✗'} for plan: {str(plan)[:60]}{RESET}")
        return entry

    @staticmethod
    def latest_entry() -> dict | None:
        if not os.path.exists(ReflectionLoop.LOG_PATH):
            return None
        try:
            with open(ReflectionLoop.LOG_PATH, 'rb') as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size == 0:
                    return None
                block_size = min(1024, size)
                f.seek(-block_size, os.SEEK_END)
                block = f.read(block_size)
                lines = block.split(b"\n")
                last_line = lines[-1]
                if not last_line and len(lines) > 1:
                    last_line = lines[-2]
                if last_line:
                    return json.loads(last_line.decode())
        except Exception:
            pass
        return None

    @staticmethod
    def analyze() -> dict | None:
        """Run Reflector analysis on the latest entry."""
        entry = ReflectionLoop.latest_entry()
        if not entry:
            return None
        from reflection.reflector import Reflector
        r = Reflector()
        diagnosis = r.analyze_failure(entry.get('result', {}))
        return {**entry, "diagnosis": diagnosis}


from reflection.self_correction import attempt_correction

__all__ = ["ReflectionLoop", "attempt_correction"]
