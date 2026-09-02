"""Wrapper for SMS commands (Termux and Windows compatible).
"""
import sys
import subprocess
import json
from typing import Optional, List

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

# Default timeout for SMS operations
SMS_TIMEOUT = 10

def get_sms_messages(limit: int = 10, type_: str = "all", address: Optional[str] = None) -> List[dict]:
    """Retrieve SMS messages."""
    if sys.platform == "win32":
        print(f"{GRAY}[SMS] SMS list is not supported on Windows desktop hardware.{RESET}")
        return []

    try:
        cmd = ['termux-sms-list', '-l', str(limit), '-t', type_]
        if address:
            cmd.extend(['-f', address])
        
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        result = subprocess.check_output(cmd, text=True, timeout=SMS_TIMEOUT)
        if result.strip():
            print(f"{GRAY}[OUT] Retrieved {len(json.loads(result))} messages.{RESET}")
            return json.loads(result)
        return []
    except subprocess.TimeoutExpired:
        print(f"{RED}[ERR] termux-sms-list timed out after {SMS_TIMEOUT}s{RESET}")
        raise RuntimeError(f"termux-sms-list timed out after {SMS_TIMEOUT}s")
    except Exception as e:
        print(f"{RED}[ERR] Failed to list SMS messages: {e}{RESET}")
        raise RuntimeError(f"Failed to list SMS messages: {e}")

def send_sms(number: str, text: str, slot: Optional[int] = None) -> bool:
    """Send an SMS message."""
    if sys.platform == "win32":
        print(f"{RED}[ERR] SMS send is not supported on Windows desktop hardware.{RESET}")
        return False

    try:
        cmd = ['termux-sms-send', '-n', number]
        if slot is not None:
            cmd.extend(['-s', str(slot)])
        cmd.append(text)
        
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        subprocess.run(cmd, check=True, timeout=SMS_TIMEOUT)
        print(f"{GRAY}[OUT] SMS sent to {number}.{RESET}")
        return True
    except subprocess.TimeoutExpired:
        print(f"{RED}[ERR] termux-sms-send timed out after {SMS_TIMEOUT}s{RESET}")
        raise RuntimeError(f"termux-sms-send timed out after {SMS_TIMEOUT}s")
    except Exception as e:
        print(f"{RED}[ERR] Failed to send SMS: {e}{RESET}")
        raise RuntimeError(f"Failed to send SMS: {e}")