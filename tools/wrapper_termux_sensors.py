"""Wrapper for Termux sensor APIs (accelerometer, gyroscope, etc.)."""
import sys
import subprocess
import json

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

SENSOR_TYPES = {
    "accelerometer": "accelerometer",
    "gyroscope": "gyroscope",
    "magnetometer": "magnetometer",
    "light": "light",
    "proximity": "proximity",
    "pressure": "pressure",
    "temperature": "temperature",
    "humidity": "humidity",
    "step_counter": "step_counter",
    "step_detector": "step_detector",
}

# `termux-sensor -l` reports raw Android sensor names (e.g. "icm4n607 acc").
# Map them onto the canonical sensor types used by get_sensor_data().
_SENSOR_NAME_HINTS = (
    ("acc",         "accelerometer"),
    ("gyro",        "gyroscope"),
    ("mag",         "magnetometer"),
    ("als",         "light"),
    ("light",       "light"),
    ("prox",        "proximity"),
    ("pressure",    "pressure"),
    ("temp",        "temperature"),
    ("humid",       "humidity"),
    ("step_count",  "step_counter"),
    ("step_detect", "step_detector"),
)


def _guess_sensor_type(raw_name: str) -> str:
    """Best-effort mapping of an Android sensor name to a canonical type."""
    lowered = raw_name.lower()
    for hint, sensor_type in _SENSOR_NAME_HINTS:
        if hint in lowered:
            return sensor_type
    return "unknown"


def list_sensors() -> list:
    """List all available sensors."""
    if sys.platform == "win32":
        return [{"name": "Windows", "type": "N/A", "note": "Sensors not available on Windows"}]

    try:
        print(f"{GRAY}[EXECUTING] termux-sensor -l{RESET}")
        result = subprocess.check_output(['termux-sensor', '-l'], text=True, timeout=10)
        if result.strip():
            print(f"{GRAY}[OUT]\n{result.strip()}{RESET}")

        # termux-sensor -l emits JSON: {"sensors": ["name", ...]}.
        # Older builds emitted a plain newline-separated list, so keep a
        # fallback for that shape.
        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            data = None

        if isinstance(data, dict) and isinstance(data.get("sensors"), list):
            raw_names = [str(s) for s in data["sensors"]]
        elif isinstance(data, list):
            raw_names = [str(s) for s in data]
        else:
            raw_names = [line.strip() for line in result.strip().split('\n') if line.strip()]

        return [
            {"name": name, "type": _guess_sensor_type(name)}
            for name in raw_names
        ]
    except Exception as e:
        print(f"{RED}[ERR] Failed to list sensors: {e}{RESET}")
        raise RuntimeError(f"Failed to list sensors: {e}")


def get_sensor_data(sensor_type: str = "accelerometer", samples: int = 1, delay: int = 100) -> dict:
    """Get sensor data for a specific sensor type.
    
    Args:
        sensor_type: Type of sensor (accelerometer, gyroscope, etc.)
        samples: Number of samples to collect
        delay: Delay between samples in milliseconds
    """
    if sys.platform == "win32":
        return {"error": "Sensors not available on Windows"}
    
    if sensor_type not in SENSOR_TYPES:
        raise ValueError(f"Unknown sensor type: {sensor_type}. Available: {list(SENSOR_TYPES.keys())}")
    
    try:
        print(f"{GRAY}[EXECUTING] termux-sensor -s {sensor_type} -n {samples} -d {delay}{RESET}")
        result = subprocess.check_output(
            ['termux-sensor', '-s', sensor_type, '-n', str(samples), '-d', str(delay)], 
            text=True, timeout=15
        )
        if result.strip():
            print(f"{GRAY}[OUT]\n{result.strip()}{RESET}")
        try:
            return json.loads(result)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"termux-sensor returned non-JSON output for '{sensor_type}': "
                f"{result.strip()[:200] or '(empty)'}"
            ) from exc
    except subprocess.TimeoutExpired:
        raise RuntimeError("termux-sensor timed out")
    except RuntimeError:
        raise
    except Exception as e:
        print(f"{RED}[ERR] Failed to get sensor data: {e}{RESET}")
        raise RuntimeError(f"Failed to get sensor data: {e}")


def start_sensor_stream(sensor_type: str = "accelerometer", delay: int = 100) -> subprocess.Popen:
    """Start a continuous sensor stream (returns Popen object).
    
    Caller must manage the process and read from stdout.
    """
    if sys.platform == "win32":
        raise RuntimeError("Sensors not available on Windows")
    
    if sensor_type not in SENSOR_TYPES:
        raise ValueError(f"Unknown sensor type: {sensor_type}")
    
    try:
        proc = subprocess.Popen(
            ['termux-sensor', '-s', sensor_type, '-d', str(delay)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return proc
    except Exception as e:
        print(f"{RED}[ERR] Failed to start sensor stream: {e}{RESET}")
        raise RuntimeError(f"Failed to start sensor stream: {e}")