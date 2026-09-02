"""Wrapper for Termux audio recording API."""
import sys
import subprocess

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

def start_recording(output_path: str = None, format: str = "aac", bitrate: int = 128000) -> subprocess.Popen:
    """Start audio recording (returns Popen object for streaming).
    
    Args:
        output_path: Path to save the recording (auto-generated if not provided)
        format: Audio format (aac, amr, 3gp, etc.)
        bitrate: Bitrate in bits per second
    """
    if sys.platform == "win32":
        raise RuntimeError("Audio recording not available on Windows")
    
    if output_path is None:
        import time
        output_path = f"/sdcard/Recordings/termux_audio_{int(time.time())}.{format}"
    
    try:
        cmd = ['termux-microphone-record', '-f', format, '-r', str(bitrate), output_path]
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return proc
    except Exception as e:
        print(f"{RED}[ERR] Failed to start recording: {e}{RESET}")
        raise RuntimeError(f"Failed to start recording: {e}")


def stop_recording(proc: subprocess.Popen) -> str:
    """Stop audio recording and return the output path."""
    if sys.platform == "win32":
        return "Audio recording not available on Windows"
    
    try:
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=5)
        if stdout:
            print(f"{GRAY}[OUT]\n{stdout.strip()}{RESET}")
        if stderr:
            print(f"{RED}[ERR]\n{stderr.strip()}{RESET}")
        return "Recording stopped"
    except Exception as e:
        print(f"{RED}[ERR] Failed to stop recording: {e}{RESET}")
        raise RuntimeError(f"Failed to stop recording: {e}")


def record_audio(output_path: str = None, format: str = "aac", bitrate: int = 128000, duration: int = 10) -> str:
    """Record audio for a specific duration.
    
    Args:
        output_path: Path to save the recording (auto-generated if not provided)
        format: Audio format (aac, amr, 3gp, etc.)
        bitrate: Bitrate in bits per second
        duration: Recording duration in seconds
    """
    if sys.platform == "win32":
        return "Audio recording not available on Windows"
    
    if output_path is None:
        import time
        output_path = f"/sdcard/Recordings/termux_audio_{int(time.time())}.{format}"
    
    try:
        cmd = ['termux-microphone-record', '-f', format, '-r', str(bitrate), '-l', str(duration), output_path]
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        result = subprocess.check_output(cmd, text=True, timeout=duration + 5)
        if result.strip():
            print(f"{GRAY}[OUT]\n{result.strip()}{RESET}")
        return f"Recording saved to {output_path}"
    except subprocess.TimeoutExpired:
        raise RuntimeError("Audio recording timed out")
    except Exception as e:
        print(f"{RED}[ERR] Failed to record audio: {e}{RESET}")
        raise RuntimeError(f"Failed to record audio: {e}")