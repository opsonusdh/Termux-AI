"""Wrapper for torch command (Termux and Windows compatible).
"""
import sys
import subprocess

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

# Default timeout for torch operations
TORCH_TIMEOUT = 5

def toggle_torch(on: bool = True) -> bool:
    """Toggle the device LED torch on or off."""
    state = "on" if on else "off"
    if sys.platform == "win32":
        print(f"{GRAY}[TORCH] Flashlight control is not available on Windows PC hardware (set to {state}).{RESET}")
        return False

    try:
        cmd = ['termux-torch', state]
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        subprocess.run(cmd, check=True, timeout=TORCH_TIMEOUT)
        print(f"{GRAY}[OUT] Torch turned {state}.{RESET}")
        return True
    except subprocess.TimeoutExpired:
        print(f"{RED}[ERR] termux-torch timed out after {TORCH_TIMEOUT}s{RESET}")
        raise RuntimeError(f"termux-torch timed out after {TORCH_TIMEOUT}s")
    except Exception as e:
        print(f"{RED}[ERR] Failed to toggle torch: {e}{RESET}")
        raise RuntimeError(f"Failed to toggle torch: {e}")