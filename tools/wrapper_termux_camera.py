"""Wrapper for Termux camera API."""
import sys
import subprocess
import base64

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

def take_photo(output_path: str = None, camera_id: int = 0) -> str:
    """Take a photo using the device camera.
    
    Args:
        output_path: Path to save the photo (auto-generated if not provided)
        camera_id: Camera ID (0 = back, 1 = front)
    """
    if sys.platform == "win32":
        return "Camera not available on Windows"
    
    if output_path is None:
        import time
        output_path = f"/sdcard/DCIM/termux_photo_{int(time.time())}.jpg"
    
    try:
        cmd = ['termux-camera-photo', '-c', str(camera_id), output_path]
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        result = subprocess.check_output(cmd, text=True, timeout=15)
        if result.strip():
            print(f"{GRAY}[OUT]\n{result.strip()}{RESET}")
        return f"Photo saved to {output_path}"
    except subprocess.TimeoutExpired:
        raise RuntimeError("Camera operation timed out")
    except Exception as e:
        print(f"{RED}[ERR] Failed to take photo: {e}{RESET}")
        raise RuntimeError(f"Failed to take photo: {e}")


def record_video(output_path: str = None, camera_id: int = 0, limit: int = 0) -> str:
    """Record a video using the device camera.
    
    Args:
        output_path: Path to save the video (auto-generated if not provided)
        camera_id: Camera ID (0 = back, 1 = front)
        limit: Maximum duration in seconds (0 = no limit)
    """
    if sys.platform == "win32":
        return "Camera not available on Windows"
    
    if output_path is None:
        import time
        output_path = f"/sdcard/DCIM/termux_video_{int(time.time())}.mp4"
    
    try:
        cmd = ['termux-camera-video', '-c', str(camera_id), output_path]
        if limit > 0:
            cmd.extend(['-l', str(limit)])
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        result = subprocess.check_output(cmd, text=True, timeout=30)
        if result.strip():
            print(f"{GRAY}[OUT]\n{result.strip()}{RESET}")
        return f"Video saved to {output_path}"
    except subprocess.TimeoutExpired:
        raise RuntimeError("Video recording timed out")
    except Exception as e:
        print(f"{RED}[ERR] Failed to record video: {e}{RESET}")
        raise RuntimeError(f"Failed to record video: {e}")


def get_camera_info() -> dict:
    """Get information about available cameras."""
    if sys.platform == "win32":
        return {"cameras": [], "note": "Camera info not available on Windows"}
    
    try:
        print(f"{GRAY}[EXECUTING] termux-camera-info{RESET}")
        result = subprocess.check_output(['termux-camera-info'], text=True, timeout=10)
        if result.strip():
            print(f"{GRAY}[OUT]\n{result.strip()}{RESET}")
        import json
        return json.loads(result)
    except Exception as e:
        print(f"{RED}[ERR] Failed to get camera info: {e}{RESET}")
        raise RuntimeError(f"Failed to get camera info: {e}")