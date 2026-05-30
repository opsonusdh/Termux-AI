import sys
import os
import subprocess
from pathlib import Path
import time
sys.dont_write_bytecode = True

BASE_DIR = Path(__file__).resolve()
WP_DIR = (
    BASE_DIR.parent.parent
    / "Termux-WP"
)

if WP_DIR.exists():
    subprocess.Popen(
        ["node", "bot"],
        cwd=WP_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

from interface import chat_loop

chat_loop()
