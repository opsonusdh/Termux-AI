"""Wrapper for battery status command (Termux and Windows compatible).
"""
import sys
import subprocess
import json

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

# Default timeout for battery status
BATTERY_TIMEOUT = 10

def get_battery_status() -> dict:
    """Retrieve battery information as JSON dict."""
    if sys.platform == "win32":
        try:
            print(f"{GRAY}[EXECUTING] PowerShell Get-CimInstance Win32_Battery{RESET}")
            ps_cmd = (
                "Get-CimInstance Win32_Battery | "
                "Select-Object EstimatedChargeRemaining, BatteryStatus, BatteryRechargeTime | "
                "ConvertTo-Json"
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=10
            )
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout)
                if isinstance(data, list) and len(data) > 0:
                    data = data[0]
                pct = data.get("EstimatedChargeRemaining", 100)
                status_code = data.get("BatteryStatus", 1)
                status = "CHARGING" if status_code == 2 else ("FULL" if pct >= 99 else "DISCHARGING")
                return {
                    "health": "GOOD",
                    "percentage": pct,
                    "plugged": "PLUGGED_AC" if status == "CHARGING" else "UNPLUGGED",
                    "status": status,
                    "temperature": 25.0
                }
            # Fallback if no laptop battery detected (e.g. desktop PC)
            return {
                "health": "GOOD",
                "percentage": 100,
                "plugged": "PLUGGED_AC",
                "status": "CHARGING (Desktop/AC)",
                "temperature": 25.0
            }
        except subprocess.TimeoutExpired:
            print(f"{RED}[ERR] Windows battery status timed out{RESET}")
            return {
                "health": "GOOD",
                "percentage": 100,
                "plugged": "PLUGGED_AC",
                "status": "FULL",
                "temperature": 25.0
            }
        except Exception as e:
            print(f"{RED}[ERR] Failed to get Windows battery status: {e}{RESET}")
            return {
                "health": "GOOD",
                "percentage": 100,
                "plugged": "PLUGGED_AC",
                "status": "FULL",
                "temperature": 25.0
            }

    try:
        print(f"{GRAY}[EXECUTING] termux-battery-status{RESET}")
        result = subprocess.check_output(['termux-battery-status'], text=True, timeout=BATTERY_TIMEOUT)
        if result.strip():
            print(f"{GRAY}[OUT]\n{result.strip()}{RESET}")
        return json.loads(result)
    except subprocess.TimeoutExpired:
        print(f"{RED}[ERR] termux-battery-status timed out after {BATTERY_TIMEOUT}s{RESET}")
        raise RuntimeError(f"termux-battery-status timed out after {BATTERY_TIMEOUT}s")
    except Exception as e:
        print(f"{RED}[ERR] Failed to get battery status: {e}{RESET}")
        raise RuntimeError(f"Failed to get battery status: {e}")