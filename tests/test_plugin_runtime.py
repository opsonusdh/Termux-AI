"""Regression tests: enabling a plugin must make it *work*, not just switch a flag.

The gap this covers: ``/plugin enable whatsapp`` used to flip a flag in
config.json and nothing else.  The model was then handed the WhatsApp tools
(``send_whatsapp_message`` and friends) while the Termux-WP node gateway —
``bot.js`` on port 3000 — was still down, so every single call failed with
"connection refused".  Enabling a plugin now starts its runtime as well.

Also covered here:

* the plugin manager's own tool (``manage_plugin``) stays in the tool
  context whatever the plugins are doing — without it the model could never
  switch one on;
* a plugin that is enabled mid-session has its tools in the *next* request
  (the catalogue is filtered per request, not once per session);
* the node gateway is started at most once.
"""
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "core") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "core"))

import paths
from core.plugin_manager import PluginManager, ALWAYS_AVAILABLE_TOOLS
from core import plugin_runtime


# ── helpers ───────────────────────────────────────────────────────────────────

def _tool_names(tools_desc):
    return [t.get("function", {}).get("name") for t in tools_desc]


@pytest.fixture
def isolated_manager(monkeypatch, tmp_path):
    """A PluginManager that writes to a throwaway config file."""
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    monkeypatch.setattr(paths, "CONFIG_FILE", str(cfg))
    return PluginManager()


class _FakeProc:
    """Stands in for a subprocess.Popen handle."""

    def __init__(self, pid=4242, returncode=None):
        self.pid = pid
        self.returncode = returncode
        self._alive = returncode is None

    def poll(self):
        return None if self._alive else self.returncode

    def terminate(self):
        self._alive = False
        self.returncode = 0

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode


# ── the manager tool is always in the tool context ───────────────────────────

def test_manage_plugin_is_always_in_the_tool_context(isolated_manager):
    """With every plugin off the manager must still be advertised."""
    from core.tools import TOOLS_DESCRIPTION

    for name in list(isolated_manager.list_plugins()):
        isolated_manager.disable(name)

    names = _tool_names(isolated_manager.filter_tools_description(TOOLS_DESCRIPTION))

    assert "manage_plugin" in names
    assert "manage_plugin" in ALWAYS_AVAILABLE_TOOLS
    # ...and no plugin tool leaked in.
    assert "send_whatsapp_message" not in names
    assert "send_gmail_message" not in names


def test_manage_plugin_survives_being_mapped_to_a_plugin(monkeypatch):
    """The guarantee is explicit, not an accident of the mapping table."""
    import core.plugin_manager as pmmod

    monkeypatch.setitem(pmmod.TOOL_PLUGIN_MAP, "manage_plugin", "gmail")
    manager = pmmod.PluginManager()
    manager.disable("gmail")

    assert manager.is_tool_allowed("manage_plugin") is True


# ── the tool context follows the plugin state ────────────────────────────────

def test_enabling_a_plugin_adds_its_tools_to_the_tool_context(isolated_manager):
    """The catalogue is filtered per request, so a mid-session enable counts."""
    from core.tools import TOOLS_DESCRIPTION

    isolated_manager.disable("whatsapp")
    before = _tool_names(isolated_manager.filter_tools_description(TOOLS_DESCRIPTION))
    assert "send_whatsapp_message" not in before

    isolated_manager.enable("whatsapp")
    after = _tool_names(isolated_manager.filter_tools_description(TOOLS_DESCRIPTION))

    assert "send_whatsapp_message" in after
    assert "get_whatsapp_chats" in after
    assert "manage_plugin" in after


# ── activation starts the runtime ────────────────────────────────────────────

def test_activate_enables_and_starts_the_runtime(isolated_manager, monkeypatch):
    """Enabling a plugin must bring its runtime up, not only flip a flag."""
    calls = []

    def fake_activate(plugin_name):
        calls.append(plugin_name)
        return {"ok": True, "state": "running",
                "message": "Termux-WP started (pid 4242)."}

    monkeypatch.setattr(plugin_runtime, "activate", fake_activate)

    result = isolated_manager.activate("whatsapp")

    assert calls == ["whatsapp"]
    assert result["ok"] is True
    assert result["state"] == "ready"
    assert "Termux-WP started" in result["message"]
    assert isolated_manager.is_enabled("whatsapp") is True
    # The enable was persisted, not just held in memory.
    assert '"enabled": true' in open(paths.CONFIG_FILE, encoding="utf-8").read().lower()


def test_activate_reports_a_runtime_that_failed_to_start(isolated_manager, monkeypatch):
    """A broken runtime is reported — the plugin is not silently useless."""
    monkeypatch.setattr(
        plugin_runtime, "activate",
        lambda name: {"ok": False, "state": "failed",
                      "message": "node is not installed."},
    )

    result = isolated_manager.activate("whatsapp")

    assert result["state"] == "enabled_runtime_failed"
    assert "node is not installed" in result["message"]
    assert isolated_manager.is_enabled("whatsapp") is True


def test_activate_unknown_plugin_is_reported(isolated_manager):
    result = isolated_manager.activate("nope")
    assert result["ok"] is False
    assert "Unknown plugin" in result["message"]


def test_deactivate_stops_the_runtime(isolated_manager, monkeypatch):
    stopped = []
    monkeypatch.setattr(plugin_runtime, "deactivate",
                        lambda name: stopped.append(name) or
                        {"ok": True, "state": "stopped",
                         "message": "Termux-WP stopped."})

    result = isolated_manager.deactivate("whatsapp")

    assert stopped == ["whatsapp"]
    assert result["enabled"] is False
    assert isolated_manager.is_enabled("whatsapp") is False


# ── the node gateway lifecycle ───────────────────────────────────────────────

def _whatsapp_manager():
    """The WhatsApp manager module — skipped when Termux-WP is not installed.

    The manager refuses to import without the Termux-WP checkout (it needs
    the qrcode-terminal module to draw a pairing code), and Termux-WP is
    optional: a fresh clone has none.  Without this guard the gateway tests
    failed on such a clone instead of skipping what they cannot check.
    """
    try:
        from whatsapp_manager import whatsapp_manager
    except (ImportError, FileNotFoundError) as exc:
        pytest.skip(f"Termux-WP is not installed: {exc}")
    return whatsapp_manager


def test_gateway_is_started_only_once(monkeypatch):
    """Starting the gateway twice must not spawn a second node process."""
    manager = _whatsapp_manager()
    spawned = []

    class _FakePopen:
        def __init__(self, *args, **kwargs):
            spawned.append(args)
            self.pid = 999
            self.returncode = None

        def poll(self):
            return None

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(manager, "_server_lock", __import__("threading").Lock())
    monkeypatch.setattr("whatsapp_manager.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("whatsapp_manager.shutil.which", lambda name: "/usr/bin/node")

    running = {"value": False}

    def _running():
        # The port opens as soon as the process exists.
        if spawned:
            running["value"] = True
        return running["value"]

    monkeypatch.setattr(manager, "server_is_running", _running)

    ok, message = manager.start_server(timeout=2, settle=0)
    assert ok is True
    assert "pid 999" in message
    assert len(spawned) == 1

    # Second call: the port is open now, so nothing is spawned again.
    ok, message = manager.start_server(timeout=2, settle=0)
    assert ok is True
    assert "already running" in message
    assert len(spawned) == 1


def test_gateway_start_reports_a_missing_node(monkeypatch):
    manager = _whatsapp_manager()
    monkeypatch.setattr(manager, "_server_lock", __import__("threading").Lock())
    monkeypatch.setattr(manager, "server_is_running", lambda: False)
    monkeypatch.setattr("whatsapp_manager.shutil.which", lambda name: None)

    ok, message = manager.start_server(timeout=2)

    assert ok is False
    assert "pkg install node" in message


def test_gateway_start_reports_an_immediate_exit(monkeypatch):
    manager = _whatsapp_manager()
    monkeypatch.setattr(manager, "_server_lock", __import__("threading").Lock())
    monkeypatch.setattr(manager, "server_is_running", lambda: False)
    monkeypatch.setattr("whatsapp_manager.shutil.which", lambda name: "/usr/bin/node")

    def _dead_proc(*args, **kwargs):
        proc = _FakeProc(returncode=1)
        return proc

    monkeypatch.setattr("whatsapp_manager.subprocess.Popen", _dead_proc)

    ok, message = manager.start_server(timeout=2)

    assert ok is False
    assert "exited immediately" in message
    assert "bot_output.log" in message


def test_gateway_start_waits_for_the_port(monkeypatch):
    """The port opening is what counts as success, not the spawn itself."""
    manager = _whatsapp_manager()
    monkeypatch.setattr(manager, "_server_lock", __import__("threading").Lock())
    monkeypatch.setattr("whatsapp_manager.shutil.which", lambda name: "/usr/bin/node")

    class _FakePopen:
        def __init__(self, *args, **kwargs):
            self.pid = 1234
            self.returncode = None
            self._alive = True

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            self._alive = False

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("whatsapp_manager.subprocess.Popen", _FakePopen)

    states = iter([False, False, True])
    monkeypatch.setattr(manager, "server_is_running", lambda: next(states, True))

    ok, message = manager.start_server(timeout=5, settle=0)
    assert ok is True
    assert "pid 1234" in message


def test_gateway_start_reports_an_exit_right_after_the_port_opens(monkeypatch):
    """A gateway that dies as the engine boots must not be called ready.

    The HTTP port opens before the WhatsApp Web engine finishes
    initialising, and that engine can take the node process down with it.
    Reporting "ready" for a process that is already on its way out was the
    bug; the start has to wait until the process survived the settle window.
    """
    manager = _whatsapp_manager()
    monkeypatch.setattr(manager, "_server_lock", __import__("threading").Lock())
    monkeypatch.setattr("whatsapp_manager.shutil.which", lambda name: "/usr/bin/node")

    state = {"spawned": False}

    class _DyingPopen:
        def __init__(self, *args, **kwargs):
            self.pid = 4242
            self.returncode = 1
            self._polls = 0

        def poll(self):
            # Alive for the first two polls, then gone.
            self._polls += 1
            return None if self._polls <= 2 else 1

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 1

    def _popen(*args, **kwargs):
        state["spawned"] = True
        return _DyingPopen()

    # The port only answers once the process exists.
    monkeypatch.setattr(manager, "server_is_running", lambda: state["spawned"])
    monkeypatch.setattr("whatsapp_manager.subprocess.Popen", _popen)

    ok, message = manager.start_server(timeout=2, settle=1)

    assert ok is False
    assert "exited right after starting" in message
    assert "bot_output.log" in message
    assert manager.server_proc is None


# ── the gateway survives a crash ──────────────────────────────────────────────

def test_gateway_start_clears_an_orphaned_browser(monkeypatch):
    """A browser left behind by a dead gateway must not block the next start.

    The WhatsApp engine runs Chromium against a profile directory.  When the
    node gateway dies, that Chromium can survive and keep the profile locked
    — and the next gateway then refuses to start with "The browser is
    already running for ...", which left the plugin dead until somebody
    killed the orphan by hand.
    """
    manager = _whatsapp_manager()
    monkeypatch.setattr(manager, "_server_lock", __import__("threading").Lock())
    monkeypatch.setattr(manager, "server_is_running", lambda: False)
    monkeypatch.setattr("whatsapp_manager.shutil.which", lambda name: "/usr/bin/node")

    cleared = []
    monkeypatch.setattr(manager, "_clear_orphaned_browsers",
                        lambda reason: cleared.append(reason) or 0)

    state = {"spawned": False}

    class _FakePopen:
        def __init__(self, *args, **kwargs):
            self.pid = 555
            self.returncode = None
            state["spawned"] = True

        def poll(self):
            return None

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("whatsapp_manager.subprocess.Popen", _FakePopen)
    # The port only answers once the process exists.
    monkeypatch.setattr(manager, "server_is_running", lambda: state["spawned"])

    ok, _ = manager.start_server(timeout=2, settle=0)

    assert ok is True
    assert cleared, "the stale profile lock was not cleared before the start"


def test_only_browsers_without_a_live_parent_count_as_orphans(monkeypatch):
    """A browser that belongs to a running gateway is not ours to kill."""
    manager = _whatsapp_manager()

    monkeypatch.setattr(manager, "_profile_browsers", lambda: [11, 22, 33])
    parents = {11: 1,                    # reparented to init -> orphan
               22: os.getpid(),         # a live gateway is still there -> not an orphan
               33: 999999}               # the parent is gone -> orphan
    monkeypatch.setattr(manager, "_parent_pid", lambda pid: parents[pid])

    orphans = manager._orphaned_browsers()

    assert orphans == [11, 33]


def test_the_watchdog_restarts_a_gateway_that_died(monkeypatch):
    """The gateway dies on its own now and then; the plugin must recover.

    Chromium giving up during initialisation takes the node gateway with it,
    and nothing used to restart it: every WhatsApp tool then answered
    "connection refused" until a human started the gateway by hand.
    """
    import threading
    import time

    manager = _whatsapp_manager()
    started = []

    class _DeadProc:
        pid = 777

        def poll(self):
            return 1

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 1

    def _fake_start_server(timeout=60.0, settle=6.0):
        started.append(time.monotonic())
        manager.server_proc = _DeadProc()
        return True, "Termux-WP started (pid 777)."

    monkeypatch.setattr(manager, "start_server", _fake_start_server)
    monkeypatch.setattr(manager, "server_is_running", lambda: False)
    monkeypatch.setattr(manager, "server_proc", _DeadProc())

    manager._ensure_supervisor()
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and not started:
            time.sleep(0.25)
    finally:
        manager._stop_supervisor()

    assert started, "the watchdog did not restart the gateway"
    manager.server_proc = None


# ── the runtime layer uses the very same manager instance ────────────────────

def test_runtime_uses_the_same_manager_instance_as_the_tools():
    """Two copies of the manager would mean two listeners and two states."""
    import core.tools as tools_mod

    if not getattr(tools_mod, "WP_AVAILABLE", False):
        pytest.skip("Termux-WP is not installed — there is no manager to compare")

    from_runtime = plugin_runtime._whatsapp_manager()
    assert from_runtime is tools_mod.whatsapp_manager


# ── the manage_plugin tool ───────────────────────────────────────────────────

class _FakeRuntimeManager:
    """A plugin manager that records what the tool asked it to do."""

    def __init__(self):
        self.calls = []
        self._enabled = {"whatsapp": False}

    def list_plugins(self):
        return {"whatsapp": {"enabled": self._enabled["whatsapp"],
                             "description": "WhatsApp messaging"}}

    def get_plugin_info(self, name):
        return {"enabled": self._enabled.get(name, False),
                "description": "WhatsApp messaging"}

    def is_enabled(self, name):
        return self._enabled.get(name, False)

    def enable(self, name):
        self._enabled[name] = True
        return True

    def disable(self, name):
        self._enabled[name] = False
        return True

    def plugin_state(self, name):
        return {"name": name, "enabled": self.is_enabled(name),
                "description": "WhatsApp messaging", "requires_setup": False,
                "authorized": True, "account": "", "client_configured": True}

    def get_enabled_plugin_tools(self, name):
        return ["send_whatsapp_message", "get_whatsapp_status"]

    def activate(self, name):
        self.calls.append(("activate", name))
        self._enabled[name] = True
        return {"ok": True, "enabled": True, "state": "ready",
                "tools": ["send_whatsapp_message", "get_whatsapp_status"],
                "message": "Plugin 'whatsapp' enabled and ready. Termux-WP started (pid 4242).",
                "runtime": {"ok": True, "state": "running",
                            "message": "Termux-WP started (pid 4242)."}}

    def deactivate(self, name):
        self.calls.append(("deactivate", name))
        self._enabled[name] = False
        return {"ok": True, "enabled": False, "state": "disabled",
                "message": "Plugin 'whatsapp' disabled.",
                "runtime": {"ok": True, "state": "stopped",
                            "message": "Termux-WP stopped."}}

    def runtime_state(self, name):
        return {"ok": True, "state": "running",
                "message": "Termux-WP is running."}


def test_manage_plugin_tool_enable_starts_the_runtime(monkeypatch):
    """The model can switch a plugin on — and knows it may continue now."""
    import core.tools as tools_mod

    fake = _FakeRuntimeManager()
    monkeypatch.setattr(tools_mod.plugin_manager_module, "plugin_manager", fake)

    result = tools_mod.manage_plugin(action="enable", plugin="whatsapp")

    assert ("activate", "whatsapp") in fake.calls
    assert "Termux-WP started" in result
    assert "tool list" in result
    # The model must not be told to wait for the user — it can go on.
    assert "without asking the user to repeat" in result


def test_manage_plugin_tool_disable_stops_the_runtime(monkeypatch):
    import core.tools as tools_mod

    fake = _FakeRuntimeManager()
    fake._enabled["whatsapp"] = True
    monkeypatch.setattr(tools_mod.plugin_manager_module, "plugin_manager", fake)

    result = tools_mod.manage_plugin(action="disable", plugin="whatsapp")

    assert ("deactivate", "whatsapp") in fake.calls
    assert "Termux-WP stopped" in result


def test_manage_plugin_tool_status_reports_the_runtime(monkeypatch):
    import core.tools as tools_mod

    fake = _FakeRuntimeManager()
    monkeypatch.setattr(tools_mod.plugin_manager_module, "plugin_manager", fake)

    result = tools_mod.manage_plugin(action="status", plugin="whatsapp")
    assert "Runtime" in result
    assert "Termux-WP is running" in result


# ── the /plugin command ──────────────────────────────────────────────────────

def test_plugin_enable_command_reports_the_runtime_and_asks_for_a_retry(capsys, monkeypatch, tmp_path):
    import core.interface as iface

    fake = _FakeRuntimeManager()
    monkeypatch.setattr(iface, "plugin_manager", fake)
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    monkeypatch.setattr(iface, "CONFIG_PATH", str(cfg))

    iface._handle_plugin_command("/plugin enable whatsapp")
    out = capsys.readouterr().out

    assert ("activate", "whatsapp") in fake.calls
    assert "Termux-WP started" in out
    assert "try again" in out.lower()
    assert "tool" in out.lower()


def test_plugin_disable_command_stops_the_runtime(capsys, monkeypatch, tmp_path):
    import core.interface as iface

    fake = _FakeRuntimeManager()
    fake._enabled["whatsapp"] = True
    monkeypatch.setattr(iface, "plugin_manager", fake)
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    monkeypatch.setattr(iface, "CONFIG_PATH", str(cfg))

    iface._handle_plugin_command("/plugin disable whatsapp")
    out = capsys.readouterr().out

    assert ("deactivate", "whatsapp") in fake.calls
    assert "Termux-WP stopped" in out


def test_plugin_status_command_shows_the_runtime(capsys, monkeypatch, tmp_path):
    import core.interface as iface

    fake = _FakeRuntimeManager()
    fake._enabled["whatsapp"] = True
    monkeypatch.setattr(iface, "plugin_manager", fake)
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    monkeypatch.setattr(iface, "CONFIG_PATH", str(cfg))

    iface._handle_plugin_command("/plugin status whatsapp")
    out = capsys.readouterr().out

    assert "Runtime" in out
    assert "Termux-WP is running" in out


# ── the startup path starts the runtime too ──────────────────────────────────

def test_startup_starts_the_runtime_when_the_plugin_is_enabled(capsys, monkeypatch):
    """An enabled plugin must be running when the app starts.

    Otherwise the WhatsApp tools are advertised at every request and fail
    every time, because the gateway only comes up when the plugin is
    switched on *again*.
    """
    import core.interface as iface

    calls = []

    class _StubManager:
        def is_enabled(self, name):
            return True

    class _StubWhatsApp:
        def ensure_running(self):
            calls.append("ensure")
            return True, "Termux-WP started."

    monkeypatch.setattr(iface, "plugin_manager", _StubManager())
    monkeypatch.setattr(iface, "whatsapp_manager", _StubWhatsApp())

    ok = iface._start_whatsapp_runtime()

    assert calls == ["ensure"]
    assert ok is True


def test_startup_reports_a_runtime_that_does_not_start(capsys, monkeypatch):
    import core.interface as iface

    class _StubManager:
        def is_enabled(self, name):
            return True

    class _StubWhatsApp:
        def ensure_running(self):
            return False, "node is missing."

    monkeypatch.setattr(iface, "plugin_manager", _StubManager())
    monkeypatch.setattr(iface, "whatsapp_manager", _StubWhatsApp())

    ok = iface._start_whatsapp_runtime()
    out = capsys.readouterr().out

    assert ok is False
    assert "node is missing" in out


def test_startup_skips_a_disabled_plugin(capsys, monkeypatch):
    import core.interface as iface

    class _StubManager:
        def is_enabled(self, name):
            return False

    class _StubWhatsApp:
        def ensure_running(self):
            raise AssertionError("must not be called for a disabled plugin")

    monkeypatch.setattr(iface, "plugin_manager", _StubManager())
    monkeypatch.setattr(iface, "whatsapp_manager", _StubWhatsApp())

    ok = iface._start_whatsapp_runtime()
    out = capsys.readouterr().out

    assert ok is False
    assert "WhatsApp plugin disabled" in out


# ── a second session would answer every message twice ────────────────────────

def test_a_second_session_is_detected(monkeypatch):
    """Two sessions share one gateway — and would each answer every message.

    Every session opens its own WebSocket listener to the Termux-WP gateway,
    so a second one handles every incoming message a second time and the
    busy auto-reply answers twice.  The sessions cannot see each other in
    memory, so they are found in the process table.
    """
    import io

    import core.interface as iface

    fake = {"/proc/999/cmdline": b"python\x00core/interface.py\x00",
            "/proc/1000/cmdline": b"python\x00core/other.py\x00",
            "/proc/1001/cmdline": b"node\x00bot.js\x00"}

    def _fake_open(path, mode="r", *args, **kwargs):
        if str(path) in fake:
            return io.BytesIO(fake[str(path)])
        raise FileNotFoundError(path)

    monkeypatch.setattr(iface.os, "listdir",
                        lambda path: ["999", "1000", "1001"])
    # The function looks ``open`` up in the module namespace first, so the
    # fake file access stays local to this test.
    monkeypatch.setattr(iface, "open", _fake_open, raising=False)
    monkeypatch.setattr(iface.os, "getpid", lambda: 1234)

    pids = iface._other_sessions()

    assert pids == [999]


# ── the dispatcher points the model at the manager ────────────────────────────

def test_dispatch_error_for_a_disabled_plugin_names_the_manager():
    """The model has to learn that it can switch the plugin on itself."""
    import core.llm_client as lc

    class _StubManager:
        def is_tool_allowed(self, name):
            return False

        def get_tool_plugin(self, name):
            return "whatsapp"

        def plugin_state(self, name):
            return {"enabled": False, "authorized": True}

    original = lc.plugin_manager
    lc.plugin_manager = _StubManager()
    try:
        message = lc._dispatch_tool({
            "function": {"name": "send_whatsapp_message", "arguments": "{}"}
        })
    finally:
        lc.plugin_manager = original

    assert "manage_plugin" in message
    assert "'whatsapp'" in message
    assert "/plugin enable whatsapp" in message
