"""Wrapper for termux-battery-status command.

Provides a Python function to retrieve battery information as JSON.
"""
import subprocess, json

# Gray debug trail colors
GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

def get_battery_status():
    """Execute `termux-battery-status` and return parsed JSON.
    Raises RuntimeError on failure.
    """
    try:
        print(f"{GRAY}[EXECUTING] termux-battery-status{RESET}")
        result = subprocess.check_output(['termux-battery-status'], text=True)
        if result.strip():
            print(f"{GRAY}[OUT]\n{result.strip()}{RESET}")
        return json.loads(result)
    except Exception as e:
        print(f"{RED}[ERR] Failed to get battery status: {e}{RESET}")
        raise RuntimeError(f"Failed to get battery status: {e}")
