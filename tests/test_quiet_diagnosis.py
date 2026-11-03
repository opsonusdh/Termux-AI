"""Regression tests: the background diagnosis must never scribble on the prompt.

Bug (reported in ``workspace/problem.txt``): starting the chat printed the
background diagnosis straight into the live prompt —

    YOU > [OUT]
               [
                  {
                     "bssid": "04:ab:08:bf:57:42",
                                     "frequency_mhz": 2412,
   ...

Two separate things went wrong.

1. ``core/interface.py`` started ``run_diagnosis()`` on a background thread at
   import time.  The platform wrappers behind the checks print an
   ``[EXECUTING] …`` / ``[OUT] …`` trace, so that trace landed in the middle
   of the user's prompt.  The *data* is already handed to the model as a
   system message (``core/interface.py::_get_diag_history``), so the trace
   was pure noise.

2. ``prompt_toolkit`` is not installed on this device, so the input handler
   falls back to ``_fallback_posix_input()``, which owns the tty in raw
   mode.  ``tty.setraw()`` clears OPOST, so the tty stops expanding a bare
   ``"\\n"`` into ``"\\r\\n"``: every line of that trace started further to the
   right than the one above it — the staircase.

Both are fixed:

  * the startup probe asks for a quiet run (``run_background_diagnosis()``),
    while an explicit ``run_diagnosis`` tool call keeps its trace;
  * the raw-mode input path puts output post-processing back, so a stray
    newline from *any* thread still returns the cursor to column 0.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ── helpers ────────────────────────────────────────────────────────────────────

def _load_wrapper(name: str):
    """Load a platform wrapper by path.

    ``import tools.wrapper_x`` is not reliable here: the ``tools`` name is
    shadowed by ``core/tools.py`` in some import paths, so the file is loaded
    directly — the same way ``core/tools.py`` loads them.
    """
    path = os.path.join(REPO_ROOT, "tools", f"wrapper_{name}.py")
    spec = importlib.util.spec_from_file_location(f"test_wrapper_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeSubprocess:
    """Stands in for the ``subprocess`` module inside a wrapper."""

    class TimeoutExpired(Exception):
        pass

    def __init__(self, payload: str):
        self.payload = payload
        self.commands = []

    def check_output(self, cmd, **kwargs):
        self.commands.append(cmd)
        return self.payload

    def run(self, cmd, **kwargs):  # pragma: no cover - not used on the happy path
        raise AssertionError(f"unexpected subprocess.run: {cmd}")


BATTERY_PAYLOAD = json.dumps({
    "present": True, "technology": "Li-ion", "health": "GOOD",
    "plugged": "UNPLUGGED", "status": "DISCHARGING", "temperature": 43.9,
    "voltage": 3000, "current": 442000, "percentage": 73, "level": 73,
})

WIFI_PAYLOAD = json.dumps([
    {"bssid": "04:ab:08:bf:57:42", "frequency_mhz": 2412, "rssi": -86,
     "ssid": "MITRA"},
])


# ── 1. the wrappers honour the quiet flag ─────────────────────────────────────

def test_the_battery_wrapper_is_silent_when_quiet(capsys, monkeypatch):
    wrapper = _load_wrapper("termux_battery_status")
    monkeypatch.setattr(wrapper, "subprocess", _FakeSubprocess(BATTERY_PAYLOAD))

    result = wrapper.get_battery_status(quiet=True)

    out = capsys.readouterr().out
    assert out == "", f"the quiet battery check wrote to the terminal: {out!r}"
    # ...and the data still comes back.
    assert result["percentage"] == 73


def test_the_battery_wrapper_traces_when_verbose(capsys, monkeypatch):
    """An explicit tool call keeps its trace — that is the point of it."""
    wrapper = _load_wrapper("termux_battery_status")
    monkeypatch.setattr(wrapper, "subprocess", _FakeSubprocess(BATTERY_PAYLOAD))

    wrapper.get_battery_status()

    out = capsys.readouterr().out
    assert "[EXECUTING]" in out
    assert "[OUT]" in out
    assert "percentage" in out


def test_the_wifi_wrapper_is_silent_when_quiet(capsys, monkeypatch):
    wrapper = _load_wrapper("termux_wifi_scaninfo")
    monkeypatch.setattr(wrapper, "subprocess", _FakeSubprocess(WIFI_PAYLOAD))

    result = wrapper.get_wifi_scan_info(quiet=True)

    out = capsys.readouterr().out
    assert out == "", f"the quiet wifi check wrote to the terminal: {out!r}"
    assert result[0]["ssid"] == "MITRA"


def test_the_wifi_wrapper_traces_when_verbose(capsys, monkeypatch):
    wrapper = _load_wrapper("termux_wifi_scaninfo")
    monkeypatch.setattr(wrapper, "subprocess", _FakeSubprocess(WIFI_PAYLOAD))

    wrapper.get_wifi_scan_info()

    out = capsys.readouterr().out
    assert "[EXECUTING]" in out
    assert "[OUT]" in out
    assert "MITRA" in out


# ── 2. the background probe asks the checks to be quiet ───────────────────────

def test_the_background_probe_asks_every_check_for_silence(monkeypatch):
    from core import tools as ct

    seen = {}

    def _fake(name):
        def _check(quiet=False):
            seen[name] = quiet
            return {"ok": True}
        return _check

    monkeypatch.setattr(ct, "CHECKS", {
        "battery": _fake("battery"),
        "network": _fake("network"),
    })

    ct.run_background_diagnosis()

    assert seen == {"battery": True, "network": True}


def test_an_explicit_diagnosis_still_traces(monkeypatch, capsys):
    """The LLM-facing tool call must keep showing what it ran."""
    from core import tools as ct

    def _check(quiet=False):
        print(f"[EXECUTING] termux-battery-status quiet={quiet}")
        return {"ok": True}

    monkeypatch.setattr(ct, "CHECKS", {"battery": _check})

    ct.run_diagnosis()

    out = capsys.readouterr().out
    assert "[EXECUTING]" in out
    assert "quiet=False" in out


def test_the_startup_probe_submits_the_quiet_diagnosis():
    """The chat loop must not start the noisy diagnosis again."""
    source = open(os.path.join(REPO_ROOT, "core", "interface.py"),
                  encoding="utf-8").read()
    assert "submit(run_diagnosis)" not in source, (
        "the startup probe must use run_background_diagnosis()"
    )
    assert "submit(run_background_diagnosis)" in source


def test_the_interface_imports_the_quiet_probe():
    import core.interface as iface
    from core import tools as ct

    assert iface.run_background_diagnosis is ct.run_background_diagnosis


# ── 3. the raw-mode input path cannot staircase ──────────────────────────────

PTY_CHILD = r'''
import os, sys, threading, time
sys.path.insert(0, %r + "/core")
sys.path.insert(0, %r)

signal_file = %r

def background():
    # Long enough for the input handler to have put the tty in raw mode.
    time.sleep(1.5)
    sys.stdout.write("LINE-ONE\nLINE-TWO\n")
    sys.stdout.flush()
    with open(signal_file, "w") as fh:
        fh.write("done")

threading.Thread(target=background, daemon=True).start()

from core.input_handler import get_interactive_input
try:
    get_interactive_input("\nYOU > ")
except Exception:
    pass
'''


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX tty only")
def test_a_background_newline_does_not_staircase_the_terminal(tmp_path):
    """A stray "\\n" from another thread must still return to column 0.

    In raw mode the tty stops expanding LF into CRLF, so the output used to
    cascade to the right.  The fix keeps output post-processing on.
    """
    import fcntl
    import pty
    import select
    import struct
    import termios

    signal_file = str(tmp_path / "signal")
    child = PTY_CHILD % (REPO_ROOT, REPO_ROOT, signal_file)

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 80, 0, 0))

    proc = subprocess.Popen(
        [sys.executable, "-c", child],
        stdin=slave, stdout=slave, stderr=slave,
        cwd=REPO_ROOT, close_fds=True,
    )
    os.close(slave)

    chunks = []
    try:
        # Wait for the background write to happen.
        deadline = time.time() + 20
        while time.time() < deadline and not os.path.exists(signal_file):
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunks.append(os.read(master, 65536))
                except OSError:
                    break

        # Submit the prompt so the child finishes.
        os.write(master, b"\r")

        deadline = time.time() + 15
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            ready, _, _ = select.select([master], [], [], 0.2)
            if not ready:
                continue
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()
        os.close(master)

    raw = b"".join(chunks)

    assert b"LINE-ONE" in raw, f"the background write never arrived: {raw!r}"
    assert b"LINE-ONE\r\nLINE-TWO\r\n" in raw, (
        "the newline was not translated — the output is staircasing again:\n"
        + raw.decode("utf-8", "replace")
    )
