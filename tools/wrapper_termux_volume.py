"""Wrapper for termux-volume command.
"""
import subprocess
import json

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

def get_volume_info() -> list:
    """Execute `termux-volume` and return parsed JSON containing stream levels."""
    try:
        print(f"{GRAY}[EXECUTING] termux-volume{RESET}")
        result = subprocess.run(['termux-volume'], capture_output=True, text=True, check=True)
        if result.stdout.strip():
            print(f"{GRAY}[OUT]\n{result.stdout.strip()}{RESET}")
            return json.loads(result.stdout)
        return []
    except Exception as e:
        print(f"{RED}[ERR] Failed to get volume info: {e}{RESET}")
        raise RuntimeError(f"Failed to get volume info: {e}")

def set_volume(stream: str, volume: int) -> bool:
    """Set the volume level for a specific stream.
    Valid streams: alarm, music, notification, ring, system, call
    """
    valid_streams = {"alarm", "music", "notification", "ring", "system", "call"}
    if stream.lower() not in valid_streams:
        raise ValueError(f"Invalid stream: {stream}. Must be one of {valid_streams}")

    try:
        cmd = ['termux-volume', stream.lower(), str(volume)]
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        subprocess.run(cmd, check=True)
        print(f"{GRAY}[OUT] Volume for '{stream}' set to {volume}.{RESET}")
        return True
    except Exception as e:
        print(f"{RED}[ERR] Failed to set volume: {e}{RESET}")
        raise RuntimeError(f"Failed to set volume: {e}")
