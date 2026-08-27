"""Utility wrappers for system tools (Termux and Windows compatible).
Provides Python functions that invoke native OS utilities (termux-*, PowerShell/Windows API)
with robust cross-platform fallbacks.
"""

import sys
import os
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
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            print(f"{GRAY}[OUT]\n{proc.stdout.strip()}{RESET}")
        elif proc.returncode != 0 and proc.stderr.strip():
            print(f"{RED}[ERR]\n{proc.stderr.strip()}{RESET}")
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        err = f"Command binary not found: {cmd[0]}"
        print(f"{RED}[ERR] {err}{RESET}")
        return 127, "", err
    except Exception as e:
        err = str(e)
        print(f"{RED}[ERR] {err}{RESET}")
        return 1, "", err

def notify(title: str, content: str) -> Tuple[int, str, str]:
    """Send a system notification."""
    if sys.platform == "win32":
        # Windows PowerShell notification Toast / Popup fallback
        ps_code = f"""
        [reflection.assembly]::loadwithpartialname('System.Windows.Forms') | Out-Null
        $notification = New-Object System.Windows.Forms.NotifyIcon
        $notification.Icon = [System.Drawing.SystemIcons]::Information
        $notification.BalloonTipTitle = '{title.replace("'", "''")}'
        $notification.BalloonTipText = '{content.replace("'", "''")}'
        $notification.Visible = $True
        $notification.ShowBalloonTip(5000)
        Start-Sleep -s 1
        $notification.Dispose()
        """
        return _run_cmd(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_code])
    return _run_cmd(['termux-notification', '-t', title, '-c', content])

def toast(message: str) -> Tuple[int, str, str]:
    """Show a toast message."""
    if sys.platform == "win32":
        ps_code = f"""
        [reflection.assembly]::loadwithpartialname('System.Windows.Forms') | Out-Null
        [System.Windows.Forms.MessageBox]::Show('{message.replace("'", "''")}', 'Toast')
        """
        return _run_cmd(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_code])
    return _run_cmd(['termux-toast', message])

def dialog(message: str, title: Optional[str] = None) -> Tuple[int, str, str]:
    """Display an input dialog and return response on stdout."""
    if sys.platform == "win32":
        dlg_title = title or "Input Required"
        ps_code = f"""
        [reflection.assembly]::loadwithpartialname('Microsoft.VisualBasic') | Out-Null
        $res = [Microsoft.VisualBasic.Interaction]::InputBox('{message.replace("'", "''")}', '{dlg_title.replace("'", "''")}', '')
        Write-Output $res
        """
        return _run_cmd(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_code])
    cmd = ['termux-dialog', '-i', message]
    if title:
        cmd.extend(['-t', title])
    return _run_cmd(cmd)

def tts_speak(text: str, engine: Optional[str] = None) -> Tuple[int, str, str]:
    """Speak text using TTS engine."""
    if sys.platform == "win32":
        ps_code = f"""
        Add-Type -AssemblyName System.Speech
        $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
        $synth.Speak('{text.replace("'", "''")}')
        """
        return _run_cmd(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_code])
    cmd = ['termux-tts-speak', text]
    if engine:
        cmd.extend(['-e', engine])
    return _run_cmd(cmd)
