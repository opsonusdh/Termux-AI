"""Wrapper for clipboard commands (get/set) - Termux and Windows compatible.
"""
import sys
import subprocess

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

# Default timeout for clipboard operations
CLIPBOARD_TIMEOUT = 5

def get_clipboard() -> str:
    """Get the current system clipboard content."""
    if sys.platform == "win32":
        try:
            print(f"{GRAY}[EXECUTING] PowerShell Get-Clipboard{RESET}")
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard"],
                capture_output=True, text=True, timeout=CLIPBOARD_TIMEOUT
            )
            out = res.stdout.strip()
            print(f"{GRAY}[OUT] Retrieved {len(out)} chars from Windows clipboard.{RESET}")
            return out
        except subprocess.TimeoutExpired:
            print(f"{RED}[ERR] Windows Get-Clipboard timed out{RESET}")
            return ""
        except Exception as e:
            print(f"{RED}[ERR] Failed to get Windows clipboard: {e}{RESET}")
            return ""

    try:
        print(f"{GRAY}[EXECUTING] termux-clipboard-get{RESET}")
        result = subprocess.run(['termux-clipboard-get'], capture_output=True, text=True, check=True, timeout=CLIPBOARD_TIMEOUT)
        out = result.stdout
        print(f"{GRAY}[OUT] Retrieved {len(out)} chars from clipboard.{RESET}")
        return out
    except subprocess.TimeoutExpired:
        print(f"{RED}[ERR] termux-clipboard-get timed out after {CLIPBOARD_TIMEOUT}s{RESET}")
        raise RuntimeError(f"termux-clipboard-get timed out after {CLIPBOARD_TIMEOUT}s")
    except Exception as e:
        print(f"{RED}[ERR] Failed to get clipboard: {e}{RESET}")
        raise RuntimeError(f"Failed to get clipboard: {e}")

def set_clipboard(text: str) -> bool:
    """Set the system clipboard content."""
    if sys.platform == "win32":
        try:
            print(f"{GRAY}[EXECUTING] PowerShell Set-Clipboard{RESET}")
            ps_cmd = f"Set-Clipboard -Value '{text.replace('\'', '\'\'')}'"
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                check=True, timeout=CLIPBOARD_TIMEOUT
            )
            print(f"{GRAY}[OUT] Windows clipboard content set successfully.{RESET}")
            return True
        except subprocess.TimeoutExpired:
            print(f"{RED}[ERR] Windows Set-Clipboard timed out{RESET}")
            return False
        except Exception as e:
            print(f"{RED}[ERR] Failed to set Windows clipboard: {e}{RESET}")
            return False

    try:
        print(f"{GRAY}[EXECUTING] termux-clipboard-set{RESET}")
        subprocess.run(['termux-clipboard-set', text], check=True, timeout=CLIPBOARD_TIMEOUT)
        print(f"{GRAY}[OUT] Clipboard content set successfully.{RESET}")
        return True
    except subprocess.TimeoutExpired:
        print(f"{RED}[ERR] termux-clipboard-set timed out after {CLIPBOARD_TIMEOUT}s{RESET}")
        raise RuntimeError(f"termux-clipboard-set timed out after {CLIPBOARD_TIMEOUT}s")
    except Exception as e:
        print(f"{RED}[ERR] Failed to set clipboard: {e}{RESET}")
        raise RuntimeError(f"Failed to set clipboard: {e}")