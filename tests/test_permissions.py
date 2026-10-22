"""Regression tests for the permission gate in :mod:`core.permissions`.

The gate used to split a command by cutting it at every ``;``, ``&&``,
``||`` and newline.  Because a compound block contains semicolons of its own,
the cut landed *inside* the block and the segment's command word became a
shell keyword (``do``, ``then``, …) instead of the command that actually
runs — so the destructive command inside the block was never inspected and
the whole block slipped through.

These tests pin that behaviour shut, together with the closely related
bypasses: leading ``NAME=value`` assignments, wrapper commands (``env``,
``command``, ``xargs``, …), command substitutions and ``$VAR`` path
resolution.
"""
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core import permissions  # noqa: E402


@pytest.fixture(autouse=True)
def _not_autonomous(monkeypatch):
    """Autonomous mode auto-approves everything except api.keys.

    The gate itself has to be exercised with it off.
    """
    monkeypatch.setattr(permissions, "is_autonomous", lambda: False)


def needs(cmd):
    return permissions.command_needs_permission(cmd)


# ── the reported hole: compound blocks ──────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "for i in 1 2 3; do rm -rf $HOME/x; done",
    "if true; then rm -rf /tmp/y; fi",
    "if a; then echo x; elif b; then rm -rf /tmp/q; fi",
    "while read l; do rm -rf /tmp/w; done < list",
    "until false; do rm -rf /tmp/u; done",
    "case $x in a) rm -rf /tmp/c ;; esac",
    "{ rm -rf /tmp/g; }",
    "( rm -rf /tmp/s )",
    "for i in 1 2; do if [ -f x ]; then rm -rf /tmp/n; fi; done",
])
def test_a_destructive_command_inside_a_block_is_gated(cmd):
    assert needs(cmd), f"the block was not inspected: {cmd}"


@pytest.mark.parametrize("cmd", [
    "for i in 1 2 3; do rm -rf $HOME/x; done",
    "if true; then rm -rf /tmp/y; fi",
    "case $x in a) rm -rf /tmp/c ;; esac",
])
def test_the_offending_command_is_the_one_reported(cmd):
    """The gate must point at the real command, not at the block keyword."""
    segments = permissions._split_shell_chain(cmd)
    assert all(not seg.startswith(("do ", "then ", "done", "fi", "esac"))
               for seg in segments)


# ── related bypasses ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "FOO=1 rm -rf /tmp/env",            # leading assignment hides the command
    "command rm -rf /tmp/cw",            # `command` runs another command
    "env FOO=1 rm -rf /tmp/env2",       # `env` runs another command
    "xargs rm -rf /tmp/xa",              # `xargs` runs another command
    "nohup rm -rf /tmp/nh",              # `nohup` runs another command
    "timeout 30 rm -rf /tmp/to",         # `timeout` runs another command
    "echo $(rm -rf /tmp/sub)",           # command substitution
    "echo `rm -rf /tmp/bt`",             # backtick substitution
    "ls | tee /tmp/teeout",             # the right side of a pipe
    "sleep 1 & rm -rf /tmp/bg",          # background operator
    "rm -rf $HOME/x",                    # a variable path
    "bash -c 'rm /tmp/inline'",          # inline script
])
def test_a_hidden_destructive_command_is_gated(cmd):
    assert needs(cmd), f"bypass: {cmd}"


def test_doas_is_forbidden_like_sudo():
    assert needs("doas rm -rf /tmp/x")


# ── no false positives ───────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "ls -la",
    "echo hello",
    "cat README.md",
    "git status",
    "grep -r foo .",
    "for i in 1 2 3; do echo $i; done",
    "if true; then echo yes; fi",
    "cat file | grep x | wc -l",
    "echo 'a && b'",
    'echo "a ; b"',
    "pip install requests",
    "echo x > workspace/out.txt",
    "mkdir -p workspace/newdir",
    "python3 -c 'print(1)'",
    "bash -c 'echo hi'",
    "command -v ls",
    "timeout 5 echo hi",
    "env FOO=bar echo hi",
    "xargs echo hi",
])
def test_harmless_commands_are_not_gated(cmd):
    assert not needs(cmd), f"false positive: {cmd}"


# ── the rules that must keep working ────────────────────────────────────────

def test_api_keys_is_always_gated():
    assert needs("cat config/api.keys")
    assert needs("grep -r api.keys .")


def test_api_keys_template_is_allowed():
    assert not needs("cat config/api.keys.template")


def test_writes_into_core_are_gated():
    assert needs("echo x > core/tools.py")


def test_writes_into_termux_stt_are_gated():
    assert needs("echo x > Termux-STT/foo")


def test_writes_outside_the_root_are_gated():
    assert needs("echo x > /tmp/outside")


def test_forbidden_system_commands_are_gated():
    assert needs("sudo reboot")
    assert needs("su -c 'ls'")


def test_a_working_directory_change_is_still_gated():
    """Pre-existing policy, pinned so the refactor cannot quietly drop it."""
    assert needs("cd workspace && ls")


# ── api.keys stays gated inside blocks too ───────────────────────────────────

def test_api_keys_inside_a_block_is_gated():
    assert needs("for f in config/api.keys; do cat $f; done")


def test_api_keys_inside_a_substitution_is_gated():
    assert needs("echo $(cat config/api.keys)")
