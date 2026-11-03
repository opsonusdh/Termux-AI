"""Regression tests: the WhatsApp plugin must not talk in the middle of a chat.

Three complaints drove this file, all of them about the terminal showing
things nobody asked for:

  1. *"when we didn't set up whatsapp, it constantly shows the QR throughout
     all sessions"* — the pairing code was rendered straight into the chat
     every time the gateway emitted it, and the gateway emits it often: the
     QR rotates every few seconds, and the WebSocket reconnects every five,
     each reconnect replaying the current status (and the current QR).
  2. *"after I logged in, it still shows connected and my number"* — the
     `SYSTEM_READY` event printed "Connected and ready. My number: …" into
     the middle of the session and published the owner's number into the
     scrollback.
  3. *"if WhatsApp is set up and the user asks for the QR again, kill the
     process and disable the plugin"* — asking for a pairing code on an
     already-linked session now resets the plugin instead of pretending a
     fresh code exists.

The pairing code is a live secret — whoever scans it gets read/write
access to the owner's account — so it is *captured* by the manager and
rendered only when the user explicitly asks for it.
"""
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "core") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "core"))

from core import plugin_runtime
from core.whatsapp_manager import WhatsAppManager

# ``tests/conftest.py`` stubs ``plugin_runtime.setup`` for every test — the
# real one starts a gateway, and the pairing path kills one.  The original
# function is captured here at import time (before the fixture runs) so the
# pairing behaviour can still be exercised.
_REAL_SETUP = plugin_runtime.setup


# ── helpers ───────────────────────────────────────────────────────────────────

QR_PAYLOAD = "2@abcdefghijklmnopqrstuv==,QR-PAYLOAD"


def _event(event_type, payload):
    """A gateway event as the WebSocket delivers it."""
    return json.dumps({"event": event_type, "payload": payload})


class _FakeWS:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeManager:
    """A stand-in for the WhatsApp manager (no processes, no network)."""

    def __init__(self, state="QR_REQUIRED", qr=QR_PAYLOAD):
        self.state = state
        self.qr = qr
        self.shown = []
        self.killed = 0
        self.started = 0

    def ensure_running(self):
        self.started += 1
        return True, "Termux-WP started (pid 4242)."

    def gateway_state(self):
        return {"state": self.state, "qr": self.qr, "reachable": True}

    def is_paired(self):
        return self.state == "READY"

    def wait_for_pairing_state(self, timeout=60.0, poll=1.0):
        return self.state

    def wait_for_qr(self, timeout=30.0):
        return self.qr

    def show_qr(self, qr=None):
        self.shown.append(qr)
        return True

    def kill_gateway(self):
        self.killed += 1
        self.state = "STOPPED"
        return 1, "Termux-WP stopped (1 process(es) killed)."


@pytest.fixture
def enabled_calls(monkeypatch):
    """Record the plugin enable/disable calls instead of touching config.json."""
    calls = []
    monkeypatch.setattr(
        plugin_runtime, "_set_plugin_enabled",
        lambda name, on: calls.append((name, bool(on))) or True,
    )
    return calls


# ── 1. the pairing code is captured, never printed ────────────────────────────

def test_a_pairing_qr_event_prints_nothing(capsys):
    """The event loop must stay silent when the gateway asks for a scan."""
    manager = WhatsAppManager()

    manager._on_message(None, _event("SYSTEM_QR_REQUIRED",
                                     {"qr": QR_PAYLOAD, "info": "Scan authorization required."}))

    out = capsys.readouterr().out
    assert out == "", f"the pairing event wrote to the terminal: {out!r}"
    # ...but the code was kept, so it can be shown when it is asked for.
    assert manager.current_qr() == QR_PAYLOAD


def test_the_status_event_does_not_reprint_the_qr(capsys):
    """The gateway replays its status on every reconnect — with the QR in it.

    This is the path that made the code show up over and over.
    """
    manager = WhatsAppManager()

    for _ in range(5):
        manager._on_message(None, _event("SYSTEM_STATUS",
                                         {"state": "QR_REQUIRED", "qr": QR_PAYLOAD}))

    out = capsys.readouterr().out
    assert out == "", f"the status event wrote to the terminal: {out!r}"
    assert manager.current_qr() == QR_PAYLOAD


def test_the_qr_is_rendered_only_when_it_is_asked_for(capsys, monkeypatch):
    """show_qr() is the single place the code is drawn."""
    manager = WhatsAppManager()
    manager._capture_qr(QR_PAYLOAD)

    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        print("QR-ART")
        return None

    monkeypatch.setattr("core.whatsapp_manager.subprocess.run", _fake_run)

    assert manager.show_qr() is True

    out = capsys.readouterr().out
    assert "QR-ART" in out
    assert len(calls) == 1
    assert calls[0][1]["env"]["QR_CODE"] == QR_PAYLOAD


def test_show_qr_without_a_code_does_nothing(capsys):
    manager = WhatsAppManager()
    assert manager.show_qr() is False
    assert capsys.readouterr().out == ""


def test_a_linked_session_forgets_the_pairing_code():
    """Once the session is linked the code is worthless — drop it."""
    manager = WhatsAppManager()
    manager._capture_qr(QR_PAYLOAD)
    assert manager.current_qr() == QR_PAYLOAD

    manager._on_message(None, _event("SYSTEM_READY", {"myNumber": "919876543210"}))

    assert manager.current_qr() is None


# ── 2. "Connected and ready. My number: …" is gone ───────────────────────────

def test_connecting_does_not_print_the_number(capsys):
    """The owner's number must not land in the middle of the chat."""
    manager = WhatsAppManager()

    manager._on_message(None, _event("SYSTEM_READY",
                                     {"status": "online", "myNumber": "919876543210"}))

    out = capsys.readouterr().out
    assert out == "", f"the ready event wrote to the terminal: {out!r}"
    assert "Connected and ready" not in out
    assert "919876543210" not in out
    # The number is still kept — the group-mention check needs it.
    assert manager.my_number == "919876543210"


def test_the_connected_banner_is_gone_from_the_source():
    """A guard against the banner creeping back in."""
    source = open(os.path.join(REPO_ROOT, "core", "whatsapp_manager.py"),
                  encoding="utf-8").read()
    assert "Connected and ready" not in source
    assert "My number:" not in source


def test_a_status_change_stays_quiet(capsys):
    manager = WhatsAppManager()
    manager._on_message(None, _event("SYSTEM_STATUS", {"state": "INITIALIZING"}))
    assert capsys.readouterr().out == ""


# ── 3. the pairing path: show the code, or reset the plugin ──────────────────

def test_setup_on_an_unpaired_gateway_shows_the_qr_once(monkeypatch, enabled_calls):
    """Not linked yet → the code goes on the user's screen, once."""
    fake = _FakeManager(state="QR_REQUIRED")
    monkeypatch.setattr(plugin_runtime, "_whatsapp_manager", lambda: fake)

    result = _REAL_SETUP("whatsapp")

    assert result["ok"] is True
    assert result["state"] == "qr_shown"
    assert fake.shown == [QR_PAYLOAD]
    assert ("whatsapp", True) in enabled_calls


def test_setup_on_a_linked_gateway_kills_it_and_disables_the_plugin(
        monkeypatch, enabled_calls):
    """Already linked → no code is issued; the plugin is reset instead."""
    fake = _FakeManager(state="READY")
    monkeypatch.setattr(plugin_runtime, "_whatsapp_manager", lambda: fake)

    result = _REAL_SETUP("whatsapp")

    assert result["ok"] is True
    assert result["state"] == "reset"
    assert fake.killed == 1
    assert fake.shown == [], "a linked session must not be handed a pairing code"
    assert ("whatsapp", False) in enabled_calls


def test_setup_reports_a_gateway_that_will_not_start(monkeypatch, enabled_calls):
    class _Broken(_FakeManager):
        def ensure_running(self):
            return False, "Termux-WP exited immediately (code 1)."

    monkeypatch.setattr(plugin_runtime, "_whatsapp_manager", lambda: _Broken())

    result = _REAL_SETUP("whatsapp")

    assert result["ok"] is False
    assert "exited immediately" in result["message"]


def test_setup_reports_a_missing_pairing_code(monkeypatch, enabled_calls):
    class _Silent(_FakeManager):
        def gateway_state(self):
            return {"state": "QR_REQUIRED", "qr": None, "reachable": True}

        def wait_for_qr(self, timeout=30.0):
            return None

    monkeypatch.setattr(plugin_runtime, "_whatsapp_manager", lambda: _Silent())

    result = _REAL_SETUP("whatsapp")

    assert result["ok"] is False
    assert result["state"] == "no_qr"


def test_setup_falls_back_to_the_code_in_the_gateway_status(monkeypatch, enabled_calls):
    """The WebSocket event can be missed — the gateway carries the code too."""
    class _MissedEvent(_FakeManager):
        def wait_for_qr(self, timeout=30.0):
            return None

    fake = _MissedEvent(state="QR_REQUIRED")
    monkeypatch.setattr(plugin_runtime, "_whatsapp_manager", lambda: fake)

    result = _REAL_SETUP("whatsapp")

    assert result["ok"] is True
    assert result["state"] == "qr_shown"
    assert fake.shown == [QR_PAYLOAD]


def test_setup_waits_for_the_pairing_state_before_deciding(monkeypatch, enabled_calls):
    """The gateway opens its port long before the engine has booted.

    Deciding while the state is still INITIALIZING would hand out a
    pairing code for a session that is in fact already linked.
    """
    fake = _FakeManager(state="INITIALIZING")
    monkeypatch.setattr(plugin_runtime, "_whatsapp_manager", lambda: fake)

    result = _REAL_SETUP("whatsapp")

    assert result["ok"] is False
    assert result["state"] == "unknown_state"
    assert "INITIALIZING" in result["message"]
    assert fake.shown == []
    assert fake.killed == 0


def test_wait_for_pairing_state_waits_for_a_decided_state(monkeypatch):
    manager = WhatsAppManager()
    states = iter(["INITIALIZING", "INITIALIZING", "READY"])
    monkeypatch.setattr(manager, "gateway_state",
                        lambda: {"state": next(states)})

    assert manager.wait_for_pairing_state(timeout=5, poll=0.01) == "READY"


def test_wait_for_pairing_state_gives_up(monkeypatch):
    manager = WhatsAppManager()
    monkeypatch.setattr(manager, "gateway_state",
                        lambda: {"state": "INITIALIZING"})

    assert manager.wait_for_pairing_state(timeout=0.05, poll=0.01) == "INITIALIZING"


# ── 4. the listener does not outlive the plugin ───────────────────────────────

def test_stopping_the_manager_closes_the_listener():
    manager = WhatsAppManager()
    ws = _FakeWS()
    manager.running = True
    manager.ws = ws

    manager.stop()

    assert manager.running is False
    assert ws.closed is True


def test_stopping_the_server_stops_the_listener():
    """A disabled plugin must not keep listening for messages."""
    manager = WhatsAppManager()
    manager.running = True
    manager.ws = _FakeWS()
    manager.server_proc = None

    manager.stop_server()

    assert manager.running is False


# ── 5. the entry point does not start a second gateway ────────────────────────

def test_the_entry_point_does_not_spawn_its_own_gateway():
    """`python core` used to start a second bot.js and print its stdout.

    Two gateways on one WhatsApp session means every incoming message is
    handled twice — and the gateway's stdout carries the rotating pairing
    code straight into the chat.
    """
    source = open(os.path.join(REPO_ROOT, "core", "__main__.py"),
                  encoding="utf-8").read()
    assert "Popen" not in source
    assert "subprocess" not in source
