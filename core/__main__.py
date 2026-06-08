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

def ensure_termux_property() -> None:
    termux_dir = Path.home() / ".termux"
    props_file = termux_dir / "termux.properties"
    target_line = "terminal-onclick-url-open=true"

    termux_dir.mkdir(parents=True, exist_ok=True)

    if not props_file.exists():
        props_file.write_text(target_line + "\n", encoding="utf-8")
        return

    content = props_file.read_text(encoding="utf-8")
    lines = content.splitlines()

    if target_line not in lines:
        with props_file.open("a", encoding="utf-8") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write(target_line + "\n")

ensure_termux_property()

_WP_DIR = _ROOT / "Termux-WP"
wp_process = None

if _WP_DIR.exists():
    wp_process = subprocess.Popen(
        ["node", "bot"],
        cwd=str(_WP_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
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