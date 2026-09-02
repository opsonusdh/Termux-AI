"""Wrapper for Termux wallpaper API."""
import sys
import subprocess

GRAY  = "\033[90m"
RED   = "\033[31m"
RESET = "\033[0m"

_PS_WALLPAPER_CODE = r"""
Add-Type -AssemblyName System.Drawing
$img = [System.Drawing.Image]::FromFile('{image_path}')
$screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap($screen.Width, $screen.Height)
$graphics = [System.Drawing.Graphics]::FromImage($bmp)
$graphics.DrawImage($img, 0, 0, $screen.Width, $screen.Height)
$path = [System.IO.Path]::GetTempFileName() + '.bmp'
$bmp.Save($path)
$code = @'
using System;
using System.Runtime.InteropServices;
public class Wallpaper {
    [DllImport("user32.dll")] public static extern bool SystemParametersInfo(int uAction, int uParam, string lpvParam, int fuWinIni);
}
'@
Add-Type $code
[Wallpaper]::SystemParametersInfo(20, 0, $path, 3)
"""

def set_wallpaper(image_path: str, lockscreen: bool = False) -> str:
    """Set wallpaper from an image file.
    
    Args:
        image_path: Path to the image file
        lockscreen: If True, set lockscreen wallpaper instead of home screen
    """
    if sys.platform == "win32":
        # Windows wallpaper setting via PowerShell
        try:
            # Escape single quotes in path
            safe_path = image_path.replace("'", "''")
            ps_code = _PS_WALLPAPER_CODE.format(image_path=safe_path)
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_code],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                return "Wallpaper set successfully"
            else:
                raise RuntimeError(f"Failed to set wallpaper: {result.stderr}")
        except Exception as e:
            raise RuntimeError(f"Failed to set wallpaper: {e}")
    
    try:
        cmd = ['termux-wallpaper', '-f', image_path]
        if lockscreen:
            cmd.append('-l')
        print(f"{GRAY}[EXECUTING] {' '.join(cmd)}{RESET}")
        result = subprocess.check_output(cmd, text=True, timeout=10)
        if result.strip():
            print(f"{GRAY}[OUT]\n{result.strip()}{RESET}")
        return "Wallpaper set successfully"
    except subprocess.TimeoutExpired:
        raise RuntimeError("Wallpaper command timed out")
    except Exception as e:
        print(f"{RED}[ERR] Failed to set wallpaper: {e}{RESET}")
        raise RuntimeError(f"Failed to set wallpaper: {e}")


def get_wallpaper() -> str:
    """Get current wallpaper path (not directly supported, returns info)."""
    if sys.platform == "win32":
        return "Wallpaper info not available on Windows"
    
    # Termux doesn't have a direct get wallpaper command
    return "Wallpaper get not supported by termux-api. Use termux-wallpaper -f to set."