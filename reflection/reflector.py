"""
reflection/reflector.py — Failure analyser.
"""


class Reflector:
    def analyze_failure(self, task_result: dict) -> dict:
        """Produce a diagnosis and suggested fix for a failed task result."""
        if task_result.get('status') == 'failed' or task_result.get('validation') == 'Failure':
            error = task_result.get('error', task_result.get('stderr', 'unknown error'))
            return {
                "diagnosis":     f"Task failed: {error}",
                "suggested_fix": "Retry with updated parameters or check dependencies.",
            }
        return {"diagnosis": "No issues detected.", "suggested_fix": None}
