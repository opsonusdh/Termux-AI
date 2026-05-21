"""Wrapper for termux-battery-status command.

Provides a Python function to retrieve battery information as JSON.
"""
import subprocess, json

def get_battery_status():
    """Execute `termux-battery-status` and return parsed JSON.
    Raises RuntimeError on failure.
    """
    try:
        result = subprocess.check_output(['termux-battery-status'], text=True)
        return json.loads(result)
    except Exception as e:
        raise RuntimeError(f"Failed to get battery status: {e}")
