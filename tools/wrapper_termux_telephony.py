"""Wrapper for telephony deviceinfo command (Termux and Windows compatible).
"""
import sys
import os
import platform
import socket
import subprocess
import json

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

# Default timeout for telephony device info
TELEPHONY_TIMEOUT = 10

def get_telephony_device_info() -> dict:
    """Retrieve telephony / system device info as JSON dict."""
    if sys.platform == "win32":
        try:
            return {
                "device_name": socket.gethostname(),
                "os": f"Windows {platform.release()} ({platform.version()})",
                "architecture": platform.architecture()[0],
                "processor": platform.processor(),
                "phone_number": "N/A (Desktop)",
                "sim_operator_name": "N/A",
                "network_type": "Ethernet/Wi-Fi"
            }
        except Exception as e:
            return {"error": str(e)}

    try:
        print(f"{GRAY}[EXECUTING] termux-telephony-deviceinfo{RESET}")
        result = subprocess.run(['termux-telephony-deviceinfo'], capture_output=True, text=True, check=True, timeout=TELEPHONY_TIMEOUT)
        if result.stdout.strip():
            print(f"{GRAY}[OUT]\n{result.stdout.strip()}{RESET}")
            return json.loads(result.stdout)
        return {}
    except subprocess.TimeoutExpired:
        print(f"{RED}[ERR] termux-telephony-deviceinfo timed out after {TELEPHONY_TIMEOUT}s{RESET}")
        raise RuntimeError(f"termux-telephony-deviceinfo timed out after {TELEPHONY_TIMEOUT}s")
    except Exception as e:
        print(f"{RED}[ERR] Failed to get telephony device info: {e}{RESET}")
        raise RuntimeError(f"Failed to get telephony device info: {e}")