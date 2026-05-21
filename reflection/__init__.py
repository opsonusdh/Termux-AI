import json
import os
import sys
import datetime

# Ensure ai_root is in sys.path
ai_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ai_root not in sys.path:
    sys.path.append(ai_root)

import paths

class ReflectionLoop:
    """Simple reflection loop prototype.

    The loop captures the last executed plan and its result, stores them in a
    reflection log (JSON Lines) and provides a placeholder method for analysing
    the outcome.
    """

    LOG_PATH = paths.REFLECTION_LOG_FILE

    @staticmethod
    def record(plan, result, success: bool):
        entry = {
            "timestamp": datetime.datetime.utcnow().isoformat() + 'Z',
            "plan": plan,
            "result": result,
            "success": success
        }
        # Ensure logs folder exists
        os.makedirs(os.path.dirname(ReflectionLoop.LOG_PATH), exist_ok=True)
        with open(ReflectionLoop.LOG_PATH, 'a') as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    @staticmethod
    def latest_entry():
        try:
            with open(ReflectionLoop.LOG_PATH, 'rb') as f:
                f.seek(-2, os.SEEK_END)  # Find start of last line
                while f.read(1) != b"\n":
                    f.seek(-2, os.SEEK_CUR)
                last_line = f.readline().decode()
            return json.loads(last_line)
        except Exception:
            return None

    @staticmethod
    def analyze():
        """Placeholder for future analysis logic.

        Currently returns the latest entry unchanged. Future versions may apply
        heuristics or ML models to suggest corrections.
        """
        return ReflectionLoop.latest_entry()

from .reflector import Reflector
from .self_correction import attempt_correction
