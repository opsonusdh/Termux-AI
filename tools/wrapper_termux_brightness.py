"""Wrapper for termux-brightness command.

Provides a Python function to set the screen brightness.
"""
import subprocess

# Gray debug trail colors
GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

def set_brightness(brightness: str | int) -> bool:
    """Set the screen brightness.
    
    brightness: int (0-255) or the string 'auto'
    Returns True on success. Raises RuntimeError on failure.
    """
    try:
        val = str(brightness).strip()
        cmd = ['termux-brightness', val]
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        subprocess.run(cmd, check=True)
        print(f"{GRAY}[OUT] Screen brightness set to {val}.{RESET}")
        return True
    except Exception as e:
        print(f"{RED}[ERR] Failed to set brightness: {e}{RESET}")
        raise RuntimeError(f"Failed to set brightness: {e}")
