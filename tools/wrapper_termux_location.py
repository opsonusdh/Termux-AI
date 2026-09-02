"""Wrapper for location command (Termux and Windows compatible).
"""
import sys
import subprocess
import json
import urllib.request

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

# Default timeout for location (can hang if GPS/location services off)
LOCATION_TIMEOUT = 15

def get_location(provider: str = "gps", request: str = "once") -> dict:
    """Retrieve location data as JSON dict."""
    if sys.platform == "win32":
        try:
            print(f"{GRAY}[EXECUTING] IP Location Fallback for Windows{RESET}")
            req = urllib.request.Request("https://ipapi.co/json/", headers={"User-Agent": "curl/7.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                return {
                    "latitude": data.get("latitude"),
                    "longitude": data.get("longitude"),
                    "altitude": 0.0,
                    "accuracy": 1000.0,
                    "provider": "ipapi-fallback",
                    "city": data.get("city"),
                    "region": data.get("region"),
                    "country": data.get("country_name")
                }
        except Exception as e:
            print(f"{RED}[ERR] Failed to fetch Windows IP location: {e}{RESET}")
            return {"error": str(e)}

    valid_providers = {"gps", "network", "passive"}
    valid_requests = {"once", "last", "updates"}

    if provider.lower() not in valid_providers:
        raise ValueError(f"Invalid provider: {provider}. Must be one of {valid_providers}")
    if request.lower() not in valid_requests:
        raise ValueError(f"Invalid request: {request}. Must be one of {valid_requests}")

    try:
        cmd = ['termux-location', '-p', provider.lower(), '-r', request.lower()]
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=LOCATION_TIMEOUT)
        if result.stdout.strip():
            print(f"{GRAY}[OUT]\n{result.stdout.strip()}{RESET}")
            return json.loads(result.stdout)
        return {}
    except subprocess.TimeoutExpired:
        print(f"{RED}[ERR] termux-location timed out after {LOCATION_TIMEOUT}s (location services may be disabled){RESET}")
        raise RuntimeError(f"termux-location timed out after {LOCATION_TIMEOUT}s (location services may be disabled)")
    except Exception as e:
        print(f"{RED}[ERR] Failed to get location: {e}{RESET}")
        raise RuntimeError(f"Failed to get location: {e}")