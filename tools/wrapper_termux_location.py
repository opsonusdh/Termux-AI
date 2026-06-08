"""Wrapper for termux-location command.
"""
import subprocess
import json

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

def get_location(provider: str = "gps", request: str = "once") -> dict:
    """Execute `termux-location` with specified provider and request type and parse JSON."""
    valid_providers = {"gps", "network", "passive"}
    valid_requests = {"once", "last", "updates"}

    if provider.lower() not in valid_providers:
        raise ValueError(f"Invalid provider: {provider}. Must be one of {valid_providers}")
    if request.lower() not in valid_requests:
        raise ValueError(f"Invalid request: {request}. Must be one of {valid_requests}")

    try:
        cmd = ['termux-location', '-p', provider.lower(), '-r', request.lower()]
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        if result.stdout.strip():
            print(f"{GRAY}[OUT]\n{result.stdout.strip()}{RESET}")
            return json.loads(result.stdout)
        return {}
    except Exception as e:
        print(f"{RED}[ERR] Failed to get location: {e}{RESET}")
        raise RuntimeError(f"Failed to get location: {e}")
