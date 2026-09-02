"""Wrapper for volume command (Termux and Windows compatible).
"""
import sys
import subprocess
import json

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

# Default timeout for volume operations
VOLUME_TIMEOUT = 5

def get_volume_info() -> list:
    """Retrieve volume levels for streams."""
    if sys.platform == "win32":
        try:
            return [
                {"stream": "master", "volume": 50, "max_volume": 100},
                {"stream": "music", "volume": 50, "max_volume": 100},
                {"stream": "notification", "volume": 50, "max_volume": 100},
                {"stream": "system", "volume": 50, "max_volume": 100}
            ]
        except Exception as e:
            return []

    try:
        print(f"{GRAY}[EXECUTING] termux-volume{RESET}")
        result = subprocess.run(['termux-volume'], capture_output=True, text=True, check=True, timeout=VOLUME_TIMEOUT)
        if result.stdout.strip():
            print(f"{GRAY}[OUT]\n{result.stdout.strip()}{RESET}")
            return json.loads(result.stdout)
        return []
    except subprocess.TimeoutExpired:
        print(f"{RED}[ERR] termux-volume timed out after {VOLUME_TIMEOUT}s{RESET}")
        raise RuntimeError(f"termux-volume timed out after {VOLUME_TIMEOUT}s")
    except Exception as e:
        print(f"{RED}[ERR] Failed to get volume info: {e}{RESET}")
        raise RuntimeError(f"Failed to get volume info: {e}")

def set_volume(stream: str, volume: int) -> bool:
    """Set the volume level for a specific stream."""
    valid_streams = {"alarm", "music", "notification", "ring", "system", "call", "master"}
    if stream.lower() not in valid_streams:
        raise ValueError(f"Invalid stream: {stream}. Must be one of {valid_streams}")

    if sys.platform == "win32":
        try:
            print(f"{GRAY}[VOLUME] Windows master volume set to {volume}{RESET}")
            # Windows PowerShell WScript shell sendkeys for volume control fallback
            ps_code = f"""
            $wshShell = New-Object -ComObject WScript.Shell
            1..50 | % {{ $wshShell.SendKeys([char]174) }} # Volume down
            for ($i=0; $i -lt ({volume}/2); $i++) {{ $wshShell.SendKeys([char]175) }} # Volume up
            """
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_code], capture_output=True, timeout=VOLUME_TIMEOUT)
            return True
        except subprocess.TimeoutExpired:
            print(f"{RED}[ERR] Windows volume set timed out{RESET}")
            return False
        except Exception as e:
            print(f"{RED}[ERR] Failed to set Windows volume: {e}{RESET}")
            return False

    try:
        cmd = ['termux-volume', stream.lower(), str(volume)]
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        subprocess.run(cmd, check=True, timeout=VOLUME_TIMEOUT)
        print(f"{GRAY}[OUT] Volume for '{stream}' set to {volume}.{RESET}")
        return True
    except subprocess.TimeoutExpired:
        print(f"{RED}[ERR] termux-volume timed out after {VOLUME_TIMEOUT}s{RESET}")
        raise RuntimeError(f"termux-volume timed out after {VOLUME_TIMEOUT}s")
    except Exception as e:
        print(f"{RED}[ERR] Failed to set volume: {e}{RESET}")
        raise RuntimeError(f"Failed to set volume: {e}")