"""Utility wrappers for selected Termux tools.
This module provides Python functions that internally invoke the corresponding Termux command-line utilities
using subprocess, handling arguments and returning stdout/stderr.
Only a subset of verified tools is wrapped as a proof of concept.
"""

import subprocess
from typing import Tuple, Optional

# Gray debug trail colors
GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

def _run_cmd(cmd: list[str]) -> Tuple[int, str, str]:
    """Run a command and return (exit_code, stdout, stderr)."""
    cmd_str = " ".join(cmd)
    print(f"{GRAY}[EXECUTING] {cmd_str}{RESET}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        if proc.stdout.strip():
            print(f"{GRAY}[OUT]\n{proc.stdout.strip()}{RESET}")
    else:
        if proc.stderr.strip():
            print(f"{RED}[ERR]\n{proc.stderr.strip()}{RESET}")
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
