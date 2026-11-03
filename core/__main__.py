import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True

_CORE = Path(__file__).resolve().parent
_ROOT = _CORE.parent

if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

def ensure_termux_property() -> None:
    if sys.platform == "win32" or os.name == "nt":
        return
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
            f.write(target_line + "\n")

ensure_termux_property()

# The WhatsApp gateway (Termux-WP/bot.js) is started by the plugin runtime,
# not here.
#
# This module used to spawn its own `node bot` and print the gateway's
# stdout into the terminal.  That had three defects:
#
#   1. the gateway prints the rotating pairing QR to its stdout, so every
#      session that started before pairing was buried under QR art;
#   2. the read loop waited for "System Connected!" before the chat loop
#      even began, so an unpaired device never reached the prompt;
#   3. it started a *second* gateway next to the one the plugin runtime
#      starts — two WebSocket listeners on one WhatsApp session means every
#      incoming message is handled twice.
#
# The runtime owns the gateway now (core/plugin_runtime.py), and the
# pairing code is only ever rendered on request ('/plugin setup whatsapp').

from interface import chat_loop
chat_loop()
