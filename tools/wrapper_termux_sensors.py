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

def list_sensors() -> list:
    """List all available sensors."""
    if sys.platform == "win32":
        return [{"name": "Windows", "type": "N/A", "note": "Sensors not available on Windows"}]
    
    try:
        print(f"{GRAY}[EXECUTING] termux-sensor -l{RESET}")
        result = subprocess.check_output(['termux-sensor', '-l'], text=True, timeout=10)
        if result.strip():
            print(f"{GRAY}[OUT]\n{result.strip()}{RESET}")
        # Parse the output - it's typically a list of sensor names
        sensors = []
        for line in result.strip().split('\n'):
            if line.strip():
                sensors.append({"name": line.strip(), "type": "unknown"})
        return sensors
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
        return json.loads(result)
    except subprocess.TimeoutExpired:
        raise RuntimeError("termux-sensor timed out")
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