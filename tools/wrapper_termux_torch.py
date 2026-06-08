"""Wrapper for termux-torch command.
"""
import subprocess

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

def toggle_torch(on: bool = True) -> bool:
    """Toggle the device LED torch on or off."""
    state = "on" if on else "off"
    try:
        cmd = ['termux-torch', state]
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        subprocess.run(cmd, check=True)
        print(f"{GRAY}[OUT] Torch turned {state}.{RESET}")
        return True
    except Exception as e:
        print(f"{RED}[ERR] Failed to toggle torch: {e}{RESET}")
        raise RuntimeError(f"Failed to toggle torch: {e}")
