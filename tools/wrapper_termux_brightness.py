"""Wrapper for screen brightness command (Termux and Windows compatible).
"""
import sys
import subprocess

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

# Default timeout for brightness operations
BRIGHTNESS_TIMEOUT = 5

def set_brightness(brightness: str | int) -> bool:
    """Set the screen brightness.
    
    brightness: int (0-255 or 0-100 on Windows) or 'auto'
    """
    if sys.platform == "win32":
        try:
            val = str(brightness).strip()
            if val.lower() == "auto":
                pct = 50
            else:
                pct = int(val)
                if pct > 100:
                    pct = int(pct * 100 / 255)
            ps_cmd = f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1, {pct})"
            print(f"{GRAY}[EXECUTING] PowerShell WmiSetBrightness {pct}%{RESET}")
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd], check=True, timeout=BRIGHTNESS_TIMEOUT)
            print(f"{GRAY}[OUT] Windows screen brightness set to {pct}%.{RESET}")
            return True
        except subprocess.TimeoutExpired:
            print(f"{RED}[ERR] Windows brightness set timed out{RESET}")
            return False
        except Exception as e:
            print(f"{RED}[ERR] Failed to set Windows brightness: {e}{RESET}")
            return False

    try:
        val = str(brightness).strip()
        cmd = ['termux-brightness', val]
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        subprocess.run(cmd, check=True, timeout=BRIGHTNESS_TIMEOUT)
        print(f"{GRAY}[OUT] Screen brightness set to {val}.{RESET}")
        return True
    except subprocess.TimeoutExpired:
        print(f"{RED}[ERR] termux-brightness timed out after {BRIGHTNESS_TIMEOUT}s{RESET}")
        raise RuntimeError(f"termux-brightness timed out after {BRIGHTNESS_TIMEOUT}s")
    except Exception as e:
        print(f"{RED}[ERR] Failed to set brightness: {e}{RESET}")
        raise RuntimeError(f"Failed to set brightness: {e}")