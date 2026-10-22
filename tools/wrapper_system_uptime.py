"""Wrapper for retrieving system uptime information.

Cross-platform: runs `uptime` on Linux/macOS and a PowerShell equivalent on Windows.
Returns a dict with "uptime", "idle_time", and "raw" keys.
"""
import sys
import subprocess

# Default timeout for uptime command (seconds)
UPTIME_TIMEOUT = 10


def get_uptime() -> dict:
    """Retrieve system uptime and idle time.

    Returns:
        dict with keys:
            - "uptime": Human-readable uptime string (e.g. "up 2 days, 3:14").
            - "idle_time": Idle time string (e.g. " 1:23" or "no users").
            - "raw": The raw command output.
        On error, returns a dict with an "error" key describing the failure.
    """
    try:
        if sys.platform == "win32":
            # Windows: use Get-CimInstance to query Win32_OperatingSystem
            # LastBootUpTime gives boot time; uptime is derived from it.
            ps_cmd = (
                "$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime; "
                "$uptime = (Get-Date) - $boot; "
                "Write-Output \"up $($uptime.Days) days, $($uptime.Hours):$($uptime.Minutes)\"; "
                "Write-Output \"idle $($uptime.Days) days, $($uptime.Hours):$($uptime.Minutes)\""
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=UPTIME_TIMEOUT
            )
            if res.returncode == 0 and res.stdout.strip():
                lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip()]
                uptime_str = lines[0] if len(lines) > 0 else ""
                idle_str = lines[1] if len(lines) > 1 else ""
                return {
                    "uptime": uptime_str,
                    "idle_time": idle_str,
                    "raw": res.stdout.strip(),
                }
            return {
                "uptime": "",
                "idle_time": "",
                "raw": res.stdout.strip() if res.stdout else "",
                "error": f"PowerShell returned non-zero exit code: {res.returncode}",
            }

        # Linux / macOS: use the `uptime` command
        result = subprocess.run(
            ["uptime"],
            capture_output=True, text=True, timeout=UPTIME_TIMEOUT
        )
        raw = result.stdout.strip() if result.stdout else ""
        if result.returncode != 0:
            return {
                "uptime": "",
                "idle_time": "",
                "raw": raw,
                "error": f"`uptime` returned non-zero exit code: {result.returncode}",
            }

        # Parse the standard `uptime` output:
        #   " 14:32:01 up 2 days,  3:14,  2 users,  load average: 0.15, 0.10, 0.09"
        uptime_str = ""
        idle_time = ""
        if raw:
            if "up " in raw:
                # Extract from "up" to the next comma
                after_up = raw.split("up ", 1)[1] if "up " in raw else ""
                if "," in after_up:
                    uptime_str = "up " + after_up.split(",", 1)[0].strip()
                else:
                    uptime_str = "up " + after_up.strip()
            # Idle time is typically the second comma-separated field after uptime
            # e.g. "  3:14" — grab the token after the first comma
            parts = [p.strip() for p in raw.split(",")]
            if len(parts) >= 2:
                idle_time = parts[1]

        return {
            "uptime": uptime_str,
            "idle_time": idle_time,
            "raw": raw,
        }

    except subprocess.TimeoutExpired:
        return {"error": f"`uptime` timed out after {UPTIME_TIMEOUT}s"}
    except Exception as e:
        return {"error": f"Failed to get system uptime: {e}"}