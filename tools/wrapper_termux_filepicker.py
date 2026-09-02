"""Wrapper for Termux file picker and share APIs."""
import sys
import subprocess
import json

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

def pick_file(multiple: bool = False, mime_type: str = "*/*") -> list:
    """Open file picker to select file(s).
    
    Args:
        multiple: Allow multiple file selection
        mime_type: MIME type filter (e.g., "image/*", "application/pdf")
    """
    if sys.platform == "win32":
        # Windows file picker via PowerShell
        try:
            ps_code = """
            Add-Type -AssemblyName System.Windows.Forms
            $dialog = New-Object System.Windows.Forms.OpenFileDialog
            $dialog.Filter = "All Files (*.*)|*.*"
            $dialog.MultiSelect = $false
            if ($dialog.ShowDialog() -eq "OK") {
                $dialog.FileNames | ForEach-Object { Write-Output $_ }
            }
            """
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_code],
                capture_output=True, text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                files = result.stdout.strip().split('\n')
                return [{"path": f, "name": f.split('\\')[-1]} for f in files]
            return []
        except Exception as e:
            raise RuntimeError(f"Failed to open file picker: {e}")
    
    try:
        cmd = ['termux-open', '--pick']
        if multiple:
            cmd.append('--multiple')
        if mime_type:
            cmd.extend(['--mime', mime_type])
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        result = subprocess.check_output(cmd, text=True, timeout=30)
        if result.strip():
            print(f"{GRAY}[OUT]\n{result.strip()}{RESET}")
        return json.loads(result)
    except subprocess.TimeoutExpired:
        raise RuntimeError("File picker timed out")
    except Exception as e:
        print(f"{RED}[ERR] Failed to pick file: {e}{RESET}")
        raise RuntimeError(f"Failed to pick file: {e}")


def share_file(file_path: str, mime_type: str = None, subject: str = None, text: str = None) -> str:
    """Share a file using Android share sheet.
    
    Args:
        file_path: Path to the file to share
        mime_type: MIME type of the file
        subject: Subject line for the share
        text: Additional text to share
    """
    if sys.platform == "win32":
        return "Share not available on Windows"
    
    try:
        cmd = ['termux-share', '-f', file_path]
        if mime_type:
            cmd.extend(['-m', mime_type])
        if subject:
            cmd.extend(['-s', subject])
        if text:
            cmd.extend(['-t', text])
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        result = subprocess.check_output(cmd, text=True, timeout=15)
        if result.strip():
            print(f"{GRAY}[OUT]\n{result.strip()}{RESET}")
        return "File shared successfully"
    except Exception as e:
        print(f"{RED}[ERR] Failed to share file: {e}{RESET}")
        raise RuntimeError(f"Failed to share file: {e}")


def share_text(text: str, subject: str = None) -> str:
    """Share text using Android share sheet."""
    if sys.platform == "win32":
        return "Share not available on Windows"
    
    try:
        cmd = ['termux-share', '-t', text]
        if subject:
            cmd.extend(['-s', subject])
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        result = subprocess.check_output(cmd, text=True, timeout=15)
        if result.strip():
            print(f"{GRAY}[OUT]\n{result.strip()}{RESET}")
        return "Text shared successfully"
    except Exception as e:
        print(f"{RED}[ERR] Failed to share text: {e}{RESET}")
        raise RuntimeError(f"Failed to share text: {e}")