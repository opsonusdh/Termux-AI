"""Wrapper for Termux clipboard commands (get/set).
"""
import subprocess

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

def get_clipboard() -> str:
    """Get the current system clipboard content."""
    try:
        print(f"{GRAY}[EXECUTING] termux-clipboard-get{RESET}")
        result = subprocess.run(['termux-clipboard-get'], capture_output=True, text=True, check=True)
        out = result.stdout
        print(f"{GRAY}[OUT] Retrieved {len(out)} chars from clipboard.{RESET}")
        return out
    except Exception as e:
        print(f"{RED}[ERR] Failed to get clipboard: {e}{RESET}")
        raise RuntimeError(f"Failed to get clipboard: {e}")

def set_clipboard(text: str) -> bool:
    """Set the system clipboard content."""
    try:
        print(f"{GRAY}[EXECUTING] termux-clipboard-set{RESET}")
        subprocess.run(['termux-clipboard-set', text], check=True)
        print(f"{GRAY}[OUT] Clipboard content set successfully.{RESET}")
        return True
    except Exception as e:
        print(f"{RED}[ERR] Failed to set clipboard: {e}{RESET}")
        raise RuntimeError(f"Failed to set clipboard: {e}")
