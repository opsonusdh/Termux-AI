"""Wrapper for termux-telephony-deviceinfo command.
"""
import subprocess
import json

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

def get_telephony_device_info() -> dict:
    """Execute `termux-telephony-deviceinfo` and return parsed JSON."""
    try:
        print(f"{GRAY}[EXECUTING] termux-telephony-deviceinfo{RESET}")
        result = subprocess.run(['termux-telephony-deviceinfo'], capture_output=True, text=True, check=True)
        if result.stdout.strip():
            print(f"{GRAY}[OUT]\n{result.stdout.strip()}{RESET}")
            return json.loads(result.stdout)
        return {}
    except Exception as e:
        print(f"{RED}[ERR] Failed to get telephony device info: {e}{RESET}")
        raise RuntimeError(f"Failed to get telephony device info: {e}")
