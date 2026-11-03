"""Make the repository root importable so ``import core.shell_runner`` works."""
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


@pytest.fixture(autouse=True)
def _stub_plugin_runtime(monkeypatch):
    """Unit tests must never start a plugin runtime.

    Activating the WhatsApp plugin starts the real Termux-WP gateway — a
    node process with a Chromium behind it.  A test suite that does that by
    accident leaves processes behind and makes the suite depend on the
    device's WhatsApp state.  The runtime is stubbed here; a test that
    exercises it patches ``core.plugin_runtime`` itself.

    ``setup`` is stubbed for the same reason, and with more force: the
    WhatsApp pairing path *kills* the gateway when the session is already
    linked.  A unit test that reached the real gateway took the device's
    running WhatsApp session down with it.
    """
    from core import plugin_runtime

    stub = {
        "ok": True,
        "state": "stubbed",
        "message": "runtime stubbed during tests",
    }
    monkeypatch.setattr(plugin_runtime, "activate", lambda name: dict(stub))
    monkeypatch.setattr(plugin_runtime, "deactivate", lambda name: dict(stub))
    monkeypatch.setattr(plugin_runtime, "status", lambda name: dict(stub))
    monkeypatch.setattr(plugin_runtime, "setup", lambda name: dict(stub))
