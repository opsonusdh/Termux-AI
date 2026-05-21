"""Wrapper for termux-wifi-scaninfo command.

Provides a Python function to invoke the Termux utility and return its output.
"""
import subprocess, json

def get_wifi_scan_info():
    """Execute `termux-wifi-scaninfo` and parse JSON output.
    Returns a dict or raises RuntimeError on failure.
    """
    try:
        result = subprocess.check_output(['termux-wifi-scaninfo'], text=True)
        return json.loads(result)
    except Exception as e:
        raise RuntimeError(f"Failed to get Wi‑Fi scan info: {e}")
