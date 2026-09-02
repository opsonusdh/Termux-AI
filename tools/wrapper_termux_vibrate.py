"""Wrapper for vibrate command (Termux and Windows compatible).
"""
import sys
import subprocess

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

# Default timeout for vibrate operations
VIBRATE_TIMEOUT = 5

def vibrate(duration_ms: int = 1000, force: bool = False) -> bool:
    """Vibrate the device or play a haptic beep on Windows."""
    if sys.platform == "win32":
        try:
            print(f"{GRAY}[VIBRATE] Windows haptic alert ({duration_ms}ms){RESET}")
            ps_code = f"[Console]::Beep(800, {min(1000, max(100, int(duration_ms)))})"
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_code], capture_output=True, timeout=VIBRATE_TIMEOUT)
            return True
        except subprocess.TimeoutExpired:
            print(f"{RED}[ERR] Windows vibrate timed out{RESET}")
            return True
        except Exception:
            return True

    try:
        cmd = ['termux-vibrate', '-d', str(duration_ms)]
        if force:
            cmd.append('-f')
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        subprocess.run(cmd, check=True, timeout=VIBRATE_TIMEOUT)
        print(f"{GRAY}[OUT] Device vibrated.{RESET}")
        return True
    except subprocess.TimeoutExpired:
        print(f"{RED}[ERR] termux-vibrate timed out after {VIBRATE_TIMEOUT}s{RESET}")
        raise RuntimeError(f"termux-vibrate timed out after {VIBRATE_TIMEOUT}s")
    except Exception as e:
        print(f"{RED}[ERR] Failed to vibrate: {e}{RESET}")
        raise RuntimeError(f"Failed to vibrate: {e}")