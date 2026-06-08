"""Wrapper for termux-vibrate command.
"""
import subprocess

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

def vibrate(duration_ms: int = 1000, force: bool = False) -> bool:
    """Vibrate the device for specified duration in milliseconds.
    If force is True, bypass silent mode.
    """
    try:
        cmd = ['termux-vibrate', '-d', str(duration_ms)]
        if force:
            cmd.append('-f')
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        subprocess.run(cmd, check=True)
        print(f"{GRAY}[OUT] Device vibrated.{RESET}")
        return True
    except Exception as e:
        print(f"{RED}[ERR] Failed to vibrate: {e}{RESET}")
        raise RuntimeError(f"Failed to vibrate: {e}")
