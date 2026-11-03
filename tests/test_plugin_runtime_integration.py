"""Real-path integration test for WhatsApp plugin activation.

Closes the blind spot left by ``tests/conftest.py``: its autouse fixture
stubs ``core.plugin_runtime.activate/deactivate/status`` so no test ever
starts a real node process.  This module captures the real runtime
functions at import time (before any fixture runs) and restores them for
the real-path test, so the suite can no longer stay green while the
actual ``bot.js`` start path is broken.

The test skips (rather than fails) when:

* ``node`` is not on PATH,
* ``Termux-WP/bot.js`` is missing,
* ``Termux-WP/node_modules/whatsapp-web.js`` is missing, or
* a WhatsApp gateway is already listening on port 3000 (probably one the
  user started by hand -- we never kill it).
"""

import os
import shutil
import socket
import sys
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import paths
from core import plugin_runtime
from core.plugin_manager import PluginManager, ALWAYS_AVAILABLE_TOOLS


# ── capture the REAL runtime functions before any fixture stubs them ──
# conftest.py installs an autouse fixture that replaces
# core.plugin_runtime.activate/deactivate/status.  The module-level
# bindings below are taken at import time, so they still point at the
# real implementations even after a fixture has monkeypatched them.
_real_activate = plugin_runtime.activate
_real_deactivate = plugin_runtime.deactivate
_real_status = plugin_runtime.status


WP_DIR = os.path.join(REPO_ROOT, "Termux-WP")
BOT_JS = os.path.join(WP_DIR, "bot.js")
WHATSAPP_WEB_JS = os.path.join(WP_DIR, "node_modules", "whatsapp-web.js")
PORT = 3000


def _port_open(host="127.0.0.1", port=PORT, timeout=1.5):
    """True when something is actively listening on the gateway port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


needs_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is not on PATH.",
)
needs_bot = pytest.mark.skipif(
    not os.path.exists(BOT_JS),
    reason=f"Termux-WP/bot.js is missing ({BOT_JS}).",
)
needs_deps = pytest.mark.skipif(
    not os.path.exists(WHATSAPP_WEB_JS),
    reason=f"node_modules/whatsapp-web.js is missing ({WHATSAPP_WEB_JS}).",
)
needs_gateway_free = pytest.mark.skipif(
    _port_open(),
    reason=(
        "A WhatsApp gateway is already listening on port "
        f"{PORT} -- probably started by hand. Skipping so we never kill "
        "a gateway the user started manually."
    ),
)


def _wait_for(condition, deadline):
    """Poll until ``condition()`` is True or the deadline passes."""
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.5)
    return False


@needs_gateway_free
@needs_node
@needs_bot
@needs_deps
def test_real_plugin_activation_path(monkeypatch, tmp_path):
    """Exercise the real whatsapp start path end-to-end.

    No monkeypatched plugin_runtime.activate -- the real runtime runs and
    really spawns ``node bot.js``.
    """
    from core.whatsapp_manager import whatsapp_manager
    from core.tools import TOOLS_DESCRIPTION

    # Isolate config so we never clobber the user's config.json.
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    monkeypatch.setattr(paths, "CONFIG_FILE", str(cfg))
    manager = PluginManager()

    # Defeat the conftest stub: restore the REAL plugin_runtime functions.
    monkeypatch.setattr(plugin_runtime, "activate", _real_activate)
    monkeypatch.setattr(plugin_runtime, "deactivate", _real_deactivate)
    monkeypatch.setattr(plugin_runtime, "status", _real_status)

    # 3a. The gateway must not already be running (we skip above if it is,
    # but assert defensively too -- never kill a user-started gateway).
    assert whatsapp_manager.server_proc is None
    assert not _port_open(), "port 3000 should be closed before we start"

    try:
        # 3b. Start the REAL server.
        ok, message = whatsapp_manager.start_server(timeout=120)
        assert ok is True, f"start_server failed: {message}"
        assert whatsapp_manager.server_proc is not None
        assert whatsapp_manager.server_proc.pid is not None
        assert whatsapp_manager.server_proc.poll() is None, (
            "server process exited immediately after start"
        )

        # The port really accepts a TCP connection afterwards.
        assert _wait_for(_port_open, time.monotonic() + 15), (
            "port 3000 is not accepting connections after start_server returned ok"
        )

        # runtime_status reflects a live pid.
        status = whatsapp_manager.runtime_status()
        assert status["server_running"] is True
        assert status["port"] == PORT
        assert status["pid"] == whatsapp_manager.server_proc.pid

        # Enable the plugin through the REAL activation path.
        result = manager.activate("whatsapp")
        assert result["ok"] is True
        assert result["state"] == "ready", result

        # 3c. Tool-context contract with the plugin enabled.
        names = [t.get("function", {}).get("name") for t in
                 manager.filter_tools_description(TOOLS_DESCRIPTION)]
        assert "manage_plugin" in names
        assert "send_whatsapp_message" in names, names

        # manage_plugin present even when every plugin is disabled.
        for name in list(manager.list_plugins()):
            manager.disable(name)
        names_off = [t.get("function", {}).get("name") for t in
                     manager.filter_tools_description(TOOLS_DESCRIPTION)]
        assert "manage_plugin" in names_off, names_off
    finally:
        # 3d. Tear down: stop the process WE started and assert the port
        # closes -- never leave an orphan node process behind.
        whatsapp_manager.stop_server()
        assert _wait_for(lambda: not _port_open(), time.monotonic() + 15), (
            "port 3000 still open after stop_server -- possible orphan"
        )
