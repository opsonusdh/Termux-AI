"""Wrapper for Termux contacts API (Android contacts access)."""
import sys
import subprocess
import json

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

def get_contacts() -> list:
    """Retrieve all contacts as a list of dicts."""
    if sys.platform == "win32":
        # Windows doesn't have direct contacts API
        return [{"name": "Windows", "number": "N/A", "note": "Contacts not available on Windows"}]
    
    try:
        print(f"{GRAY}[EXECUTING] termux-contact-list{RESET}")
        result = subprocess.check_output(['termux-contact-list'], text=True, timeout=10)
        if result.strip():
            print(f"{GRAY}[OUT]\n{result.strip()}{RESET}")
        return json.loads(result)
    except subprocess.TimeoutExpired:
        raise RuntimeError("termux-contact-list timed out")
    except Exception as e:
        print(f"{RED}[ERR] Failed to get contacts: {e}{RESET}")
        raise RuntimeError(f"Failed to get contacts: {e}")


def get_contact_by_id(contact_id: str) -> dict:
    """Retrieve a specific contact by ID."""
    if sys.platform == "win32":
        return {"error": "Contacts not available on Windows"}
    
    try:
        print(f"{GRAY}[EXECUTING] termux-contact-get {contact_id}{RESET}")
        result = subprocess.check_output(['termux-contact-get', contact_id], text=True, timeout=10)
        if result.strip():
            print(f"{GRAY}[OUT]\n{result.strip()}{RESET}")
        return json.loads(result)
    except Exception as e:
        print(f"{RED}[ERR] Failed to get contact: {e}{RESET}")
        raise RuntimeError(f"Failed to get contact: {e}")