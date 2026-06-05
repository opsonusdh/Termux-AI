import sys
import subprocess
from pathlib import Path

sys.dont_write_bytecode = True

_CORE = Path(__file__).resolve().parent
_ROOT = _CORE.parent

if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))
if str(_ROOT) not in sys.path:
    sys.path.insert(1, str(_ROOT))

_WP_DIR = _ROOT / "Termux-WP"

wp_process = None

if _WP_DIR.exists():
    wp_process = subprocess.Popen(
        ["node", "bot"],
        cwd=str(_WP_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    for line in wp_process.stdout:
        print(line, end="")

        if "System Connected! Your phone is now sending and receiving." in line:
            break

from interface import chat_loop
chat_loop()

if wp_process:
    wp_process.terminate()
