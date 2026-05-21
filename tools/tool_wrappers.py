"""Utility wrappers for selected Termux tools.
This module provides Python functions that internally invoke the corresponding Termux command-line utilities
using subprocess, handling arguments and returning stdout/stderr.
Only a subset of verified tools is wrapped as a proof of concept.
"""

import subprocess
from typing import Tuple, Optional

def _run_cmd(cmd: list[str]) -> Tuple[int, str, str]:
    """Run a command and return (exit_code, stdout, stderr)."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr

def notify(title: str, content: str) -> Tuple[int, str, str]:
    """Send a notification using termux-notification.
    Returns (exit_code, stdout, stderr).
    """
    return _run_cmd(['termux-notification', '-t', title, '-c', content])

def toast(message: str) -> Tuple[int, str, str]:
    """Show a toast message using termux-toast."""
    return _run_cmd(['termux-toast', message])

def dialog(message: str, title: Optional[str] = None) -> Tuple[int, str, str]:
    """Display a dialog using termux-dialog.
    Returns the user's response on stdout.
    """
    cmd = ['termux-dialog', '-i', message]
    if title:
        cmd.extend(['-t', title])
    return _run_cmd(cmd)

def tts_speak(text: str, engine: Optional[str] = None) -> Tuple[int, str, str]:
    """Speak text using termux-tts-speak.
    Optionally specify a TTS engine.
    """
    cmd = ['termux-tts-speak', text]
    if engine:
        cmd.extend(['-e', engine])
    return _run_cmd(cmd)

# Example usage (commented out):
# if __name__ == '__main__':
#     notify('Test', 'This is a test notification')
#     toast('Hello from Orion')
