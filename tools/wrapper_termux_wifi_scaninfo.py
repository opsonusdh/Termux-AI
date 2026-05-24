"""Wrapper for termux-wifi-scaninfo command.

Provides a Python function to invoke the Termux utility and return its output.
"""
import subprocess, json

# Gray debug trail colors
GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

def get_wifi_scan_info():
    """Execute `termux-wifi-scaninfo` and parse JSON output.
    Returns a dict or raises RuntimeError on failure.
    """
    try:
        print(f"{GRAY}[EXECUTING] termux-wifi-scaninfo{RESET}")
        result = subprocess.check_output(['termux-wifi-scaninfo'], text=True)
        if result.strip():
            print(f"{GRAY}[OUT]\n{result.strip()}{RESET}")
        return json.loads(result)
    except Exception as e:
        print(f"{RED}[ERR] Failed to get Wi-Fi scan info: {e}{RESET}")
        raise RuntimeError(f"Failed to get Wi‑Fi scan info: {e}")
