"""Wrapper for termux-sms-list and termux-sms-send commands.

Provides Python functions to list and send SMS messages.
"""
import subprocess
import json
from typing import Optional, List

# Gray debug trail colors
GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

def get_sms_messages(limit: int = 10, type_: str = "all", address: Optional[str] = None) -> List[dict]:
    """Retrieve SMS messages.
    
    limit: Max number of messages to return.
    type_: Message type ('all'|'inbox'|'sent'|'draft'|'outbox'|'failed'|'queued').
    address: Optional phone number filter.
    Returns parsed list of SMS messages.
    """
    try:
        cmd = ['termux-sms-list', f'-l', str(limit), f'-t', type_]
        if address:
            cmd.extend(['-f', address])
        
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        result = subprocess.check_output(cmd, text=True)
        if result.strip():
            print(f"{GRAY}[OUT] Retrieved {len(json.loads(result))} messages.{RESET}")
            return json.loads(result)
        return []
    except Exception as e:
        print(f"{RED}[ERR] Failed to list SMS messages: {e}{RESET}")
        raise RuntimeError(f"Failed to list SMS messages: {e}")

def send_sms(number: str, text: str, slot: Optional[int] = None) -> bool:
    """Send an SMS message.
    
    number: Phone number (separate multiples by comma).
    text: Message content.
    slot: Optional SIM slot index.
    Returns True on success.
    """
    try:
        cmd = ['termux-sms-send', '-n', number]
        if slot is not None:
            cmd.extend(['-s', str(slot)])
        cmd.append(text)
        
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        subprocess.run(cmd, check=True)
        print(f"{GRAY}[OUT] SMS sent to {number}.{RESET}")
        return True
    except Exception as e:
        print(f"{RED}[ERR] Failed to send SMS: {e}{RESET}")
        raise RuntimeError(f"Failed to send SMS: {e}")
