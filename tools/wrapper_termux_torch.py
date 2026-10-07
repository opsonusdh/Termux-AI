"""Wrapper for torch command (Termux and Windows compatible).
Returns a consistent dict with success status and info.
"""
import sys
import subprocess
import shutil

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

# Default timeout for torch operations
TORCH_TIMEOUT = 5


def toggle_torch(on: bool = True) -> dict:
    """Toggle the device LED torch on or off.

    Returns:
        dict: {
            "success": bool,
            "state": "on" | "off",
            "message": str,
            "platform": "windows" | "termux"
        }
    """
    state = "on" if on else "off"

    # Windows: No hardware torch, return consistent structure
    if sys.platform == "win32":
        msg = f"Flashlight control is not available on Windows PC hardware (requested: {state})"
        print(f"{GRAY}[TORCH] {msg}{RESET}")
        return {
            "success": False,
            "state": state,
            "message": msg,
            "platform": "windows"
        }

    # Check if termux-torch is available
    if not shutil.which("termux-torch"):
        msg = "termux-torch command not found. Is Termux:API installed?"
        print(f"{RED}[ERR] {msg}{RESET}")
        return {
            "success": False,
            "state": state,
            "message": msg,
            "platform": "termux"
        }

    try:
        cmd = ['termux-torch', state]
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=TORCH_TIMEOUT, check=True)
        if result.stdout.strip():
            print(f"{GRAY}[OUT]\n{result.stdout.strip()}{RESET}")
        msg = f"Torch turned {state}."
        print(f"{GRAY}[OUT] {msg}{RESET}")
        return {
            "success": True,
            "state": state,
            "message": msg,
            "platform": "termux"
        }
    except subprocess.TimeoutExpired:
        msg = f"termux-torch timed out after {TORCH_TIMEOUT}s"
        print(f"{RED}[ERR] {msg}{RESET}")
        return {
            "success": False,
            "state": state,
            "message": msg,
            "platform": "termux"
        }
    except subprocess.CalledProcessError as e:
        msg = f"termux-torch failed with exit code {e.returncode}: {e.stderr.strip() if e.stderr else 'unknown error'}"
        print(f"{RED}[ERR] {msg}{RESET}")
        return {
            "success": False,
            "state": state,
            "message": msg,
            "platform": "termux"
        }
    except Exception as e:
        msg = f"Failed to toggle torch: {e}"
        print(f"{RED}[ERR] {msg}{RESET}")
        return {
            "success": False,
            "state": state,
            "message": msg,
            "platform": "termux"
        }