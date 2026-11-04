"""Tests for the push notifications the runtime sends while it waits.

Three moments make Termux-AI stop and wait for a human: a **permission**
request, a **confirmation** and a **question**.  When the terminal is in the
background — the normal state on a phone — those prompts are invisible, so
each of them pushes a system notification as long as ``notify`` is enabled in
config.json.

These tests pin:

* the flag is read from the config file and honoured,
* nothing is sent when the flag is off,
* each kind of prompt gets its own title and carries its own content,
* the notification is sent *before* the runtime blocks on ``input()``,
* a missing Termux:API package never breaks the prompt.
"""
import builtins
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "core") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "core"))

import paths  # noqa: E402
from core import app_config, notifier  # noqa: E402


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Point the config reader at a temporary config.json."""
    target = tmp_path / "config.json"
    monkeypatch.setattr(paths, "CONFIG_FILE", str(target))
    monkeypatch.setattr(paths, "CONFIG_DIR", str(tmp_path))
    return str(target)


@pytest.fixture
def sent(monkeypatch):
    """Capture every notification the runtime tries to send."""
    calls: list[dict] = []

    def _fake_run(cmd, **kwargs):
        calls.append({"cmd": list(cmd), "kwargs": kwargs})
        return None

    monkeypatch.setattr(notifier.subprocess, "run", _fake_run)
    return calls


def write_config(path, **values):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(values, fh)


# ── the flag ──────────────────────────────────────────────────────────────────

def test_notify_defaults_to_on(config_file):
    """A config.json without the key still gets notifications."""
    write_config(config_file)
    assert notifier.enabled() is True


def test_notify_off_is_honoured(config_file):
    write_config(config_file, notify=False)
    assert notifier.enabled() is False


def test_notify_on_is_honoured(config_file):
    write_config(config_file, notify=True)
    assert notifier.enabled() is True


def test_a_corrupt_config_still_answers(config_file):
    with open(config_file, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert notifier.enabled() is True


# ── pushing ───────────────────────────────────────────────────────────────────

def test_push_sends_the_title_and_content(config_file, sent):
    write_config(config_file, notify=True)
    assert notifier.push("Some title", "Some content") is True
    assert len(sent) == 1
    cmd = sent[0]["cmd"]
    assert cmd[0] == "termux-notification"
    assert "Some title" in cmd
    assert "Some content" in cmd


def test_push_is_a_no_op_when_notifications_are_off(config_file, sent):
    write_config(config_file, notify=False)
    assert notifier.push("Some title", "Some content") is False
    assert sent == []


def test_push_never_prints_to_the_terminal(config_file, sent, capsys):
    """The notification is fired while the prompt is on screen — a stray
    print there would land in the middle of the user's line."""
    write_config(config_file, notify=True)
    notifier.push("Some title", "Some content")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_a_missing_termux_api_package_does_not_raise(config_file, monkeypatch):
    write_config(config_file, notify=True)

    def _boom(cmd, **kwargs):
        raise FileNotFoundError("termux-notification")

    monkeypatch.setattr(notifier.subprocess, "run", _boom)
    assert notifier.push("Some title", "Some content") is False


def test_an_empty_body_is_not_sent(config_file, sent):
    write_config(config_file, notify=True)
    assert notifier.push("Some title", "   ") is False
    assert sent == []


def test_a_long_body_is_truncated(config_file, sent):
    write_config(config_file, notify=True)
    notifier.push("t", "x" * 5000)
    body = sent[0]["cmd"][sent[0]["cmd"].index("--content") + 1]
    assert len(body) <= notifier._MAX_CONTENT
    assert body.endswith("...")


# ── the three kinds of prompt ─────────────────────────────────────────────────

def test_permission_uses_its_own_title(config_file, sent):
    write_config(config_file, notify=True)
    notifier.permission([("rm -rf /tmp/x", "destructive")])
    cmd = sent[0]["cmd"]
    assert cmd[cmd.index("--title") + 1] == notifier.TITLE_PERMISSION
    body = cmd[cmd.index("--content") + 1]
    assert "rm -rf /tmp/x" in body
    assert "destructive" in body


def test_confirmation_uses_its_own_title(config_file, sent):
    write_config(config_file, notify=True)
    notifier.confirmation("Delete the build folder?", default=False)
    cmd = sent[0]["cmd"]
    assert cmd[cmd.index("--title") + 1] == notifier.TITLE_CONFIRMATION
    body = cmd[cmd.index("--content") + 1]
    assert "Delete the build folder?" in body
    assert "Enter for No" in body


def test_question_uses_its_own_title(config_file, sent):
    write_config(config_file, notify=True)
    notifier.question("Which database?", ["sqlite", "postgres"])
    cmd = sent[0]["cmd"]
    assert cmd[cmd.index("--title") + 1] == notifier.TITLE_QUESTION
    body = cmd[cmd.index("--content") + 1]
    assert "Which database?" in body
    assert "1. sqlite" in body
    assert "2. postgres" in body


def test_the_three_titles_are_different():
    titles = {
        notifier.TITLE_PERMISSION,
        notifier.TITLE_CONFIRMATION,
        notifier.TITLE_QUESTION,
    }
    assert len(titles) == 3


# ── the runtime actually pushes them ──────────────────────────────────────────

@pytest.fixture
def _no_autonomous(monkeypatch):
    from core import permissions
    monkeypatch.setattr(permissions, "is_autonomous", lambda: False)
    monkeypatch.setattr(permissions, "is_voice_available", lambda: False)


def test_the_permission_gate_notifies_before_it_blocks(config_file, sent,
                                                       monkeypatch,
                                                       _no_autonomous):
    """The notification has to arrive *before* the prompt blocks — otherwise
    it arrives after the answer and is pure noise."""
    from core import permissions

    write_config(config_file, notify=True)
    order: list[str] = []

    def _fake_run(cmd, **kwargs):
        order.append("notification")
        sent.append({"cmd": list(cmd), "kwargs": kwargs})
        return None

    def _fake_input(prompt=""):
        order.append("input")
        return "n"

    monkeypatch.setattr(notifier.subprocess, "run", _fake_run)
    monkeypatch.setattr(builtins, "input", _fake_input)

    approved, _reason = permissions.validate_command("rm -rf /tmp/gated")

    assert order == ["notification", "input"]
    assert approved is False


def test_the_permission_gate_is_silent_when_notifications_are_off(
        config_file, sent, monkeypatch, _no_autonomous):
    from core import permissions

    write_config(config_file, notify=False)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "n")

    permissions.validate_command("rm -rf /tmp/gated")

    assert sent == []


def test_ask_user_notifies_before_it_blocks(config_file, sent, monkeypatch):
    from core import tools

    write_config(config_file, notify=True)
    order: list[str] = []

    def _fake_run(cmd, **kwargs):
        order.append("notification")
        sent.append({"cmd": list(cmd), "kwargs": kwargs})
        return None

    def _fake_input(prompt=""):
        order.append("input")
        return "2"

    monkeypatch.setattr(notifier.subprocess, "run", _fake_run)
    monkeypatch.setattr(builtins, "input", _fake_input)

    answer = tools.ask_user("Pick one", ["alpha", "beta"])

    assert order == ["notification", "input"]
    assert answer == "beta"
    cmd = sent[0]["cmd"]
    assert cmd[cmd.index("--title") + 1] == notifier.TITLE_QUESTION


def test_confirm_notifies_before_it_blocks(config_file, sent, monkeypatch):
    from core import tools

    write_config(config_file, notify=True)
    order: list[str] = []

    def _fake_run(cmd, **kwargs):
        order.append("notification")
        sent.append({"cmd": list(cmd), "kwargs": kwargs})
        return None

    def _fake_input(prompt=""):
        order.append("input")
        return "y"

    monkeypatch.setattr(notifier.subprocess, "run", _fake_run)
    monkeypatch.setattr(builtins, "input", _fake_input)

    assert tools.confirm("Really delete it?") is True
    assert order == ["notification", "input"]
    cmd = sent[0]["cmd"]
    assert cmd[cmd.index("--title") + 1] == notifier.TITLE_CONFIRMATION


def test_ask_user_still_works_without_notifications(config_file, sent,
                                                     monkeypatch):
    """Turning the flag off must not break the prompt itself."""
    from core import tools

    write_config(config_file, notify=False)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "yes please")

    assert tools.ask_user("Anything?") == "yes please"
    assert sent == []


# ── one definition of the default config ──────────────────────────────────────

def test_the_default_config_knows_about_notify():
    assert "notify" in app_config.DEFAULT_CONFIG
    assert app_config.DEFAULT_CONFIG["notify"] is True


def test_every_module_shares_the_same_default_set():
    """The three copies of DEFAULT_CONFIG drifted; now there is one."""
    from core import permissions, tools

    assert set(permissions.DEFAULT_CONFIG) == set(app_config.DEFAULT_CONFIG)
    assert set(tools.app_config.DEFAULT_CONFIG) == set(app_config.DEFAULT_CONFIG)
    assert "notify" in permissions.DEFAULT_CONFIG

# ── the /notify command ───────────────────────────────────────────────────────
# The command is the user's handle on the flag, so it has to be honest:
# say what it did, report the state that is really in force, and prove the
# plumbing when it is switched on.

@pytest.fixture
def interface(tmp_path, monkeypatch):
    """The chat interface, pointed at a throwaway config.json."""
    from core import interface as iface

    target = tmp_path / "config.json"
    with open(target, "w", encoding="utf-8") as fh:
        json.dump({"notify": True}, fh)
    monkeypatch.setattr(iface, "CONFIG_PATH", str(target))

    # The test notification must not reach the phone.
    fired: list[tuple] = []
    monkeypatch.setattr(
        iface, "_send_notification",
        lambda title, message: fired.append((title, message)),
    )
    return iface, str(target), fired


def stored_flag(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh).get("notify")


def test_notify_false_writes_the_flag_and_says_so(interface, capsys):
    iface, path, fired = interface
    assert iface._handle_notify_command("/notify false") is True
    assert stored_flag(path) is False
    out = capsys.readouterr().out
    assert "OFF" in out
    assert fired == []


def test_notify_true_writes_the_flag_and_fires_one_test(interface, capsys):
    iface, path, fired = interface
    assert iface._handle_notify_command("/notify true") is True
    assert stored_flag(path) is True
    assert "ON" in capsys.readouterr().out
    assert len(fired) == 1
    assert fired[0][0] == "Termux-AI: Notifications enabled"


def test_notify_status_reports_the_stored_state(interface, capsys):
    iface, path, fired = interface
    iface._handle_notify_command("/notify false")
    capsys.readouterr()

    assert iface._handle_notify_command("/notify status") is True
    assert "OFF" in capsys.readouterr().out


def test_notify_status_reads_the_file_not_the_import_snapshot(interface,
                                                               capsys):
    """The notifier reads config.json at every prompt — the status command
    must not report the import-time copy, or the two can disagree."""
    iface, path, fired = interface
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"notify": False}, fh)
    iface.config = dict(iface.config, notify=True)

    iface._handle_notify_command("/notify status")
    assert "OFF" in capsys.readouterr().out


def test_notify_toggle_flips_the_stored_state(interface, capsys):
    iface, path, fired = interface

    assert iface._handle_notify_command("/notify toggle") is True
    assert stored_flag(path) is False
    assert fired == []

    assert iface._handle_notify_command("/notify toggle") is True
    assert stored_flag(path) is True
    assert len(fired) == 1
    assert "ON" in capsys.readouterr().out


def test_notify_toggle_reads_the_file_not_the_import_snapshot(interface):
    iface, path, fired = interface
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"notify": False}, fh)
    iface.config = dict(iface.config, notify=True)

    iface._handle_notify_command("/notify toggle")
    assert stored_flag(path) is True


def test_a_command_that_is_not_notify_is_not_consumed(interface):
    iface, path, fired = interface
    assert iface._handle_notify_command("/not") is False
    assert iface._handle_notify_command("/notify") is True


def test_a_notify_typo_shows_the_usage_instead_of_being_sent_to_the_model(
        interface, capsys):
    iface, path, fired = interface
    assert iface._handle_notify_command("/notify wat") is True
    out = capsys.readouterr().out
    assert "Usage:" in out
    assert "toggle" in out


def test_the_notify_command_is_offered_in_the_help(interface, capsys):
    iface, path, fired = interface
    iface._show_help()
    out = capsys.readouterr().out
    assert "/notify" in out
    assert "toggle" in out
