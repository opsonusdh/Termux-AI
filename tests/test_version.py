"""The version constant, the banner that shows it, and the changelog.

Three properties are pinned here:

* there is exactly **one** place the version is written down
  (``VERSION`` in ``core/__main__.py``), and it says 3.3.1;
* the startup banner shows that version, between the name and the
  status line — so the user can tell which build they are talking to;
* the release heading in ``CHANGELOG.md`` agrees with the constant, so
  the banner and the release notes cannot drift apart.
"""
import json
import os
import re
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "core") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "core"))

import paths
from core import runtime_log
from core.ui import plain


CHANGELOG = os.path.join(REPO_ROOT, "CHANGELOG.md")


# ── The constant ─────────────────────────────────────────────────────────────

def test_the_version_constant_lives_in_the_entry_point():
    """``VERSION`` is defined in core/__main__.py — the entry point."""
    from core import __main__ as entry

    assert entry.VERSION == "3.3.1"


def test_the_version_is_a_plain_dotted_string():
    """Nothing clever: a version the banner and the changelog can compare."""
    from core import __main__ as entry

    assert re.fullmatch(r"\d+\.\d+\.\d+", entry.VERSION)


def test_the_changelog_release_agrees_with_the_constant():
    """The newest released heading in the changelog is the version."""
    from core import __main__ as entry

    with open(CHANGELOG, encoding="utf-8") as fh:
        text = fh.read()

    released = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", text, re.MULTILINE)
    assert released, "the changelog has no released version heading"
    assert released[0] == entry.VERSION

    # The version the banner shows is this one.
    assert f"v{entry.VERSION}" in text or entry.VERSION in text


def test_the_unreleased_section_is_still_there():
    """A released version needs an empty slot for the next one."""
    with open(CHANGELOG, encoding="utf-8") as fh:
        text = fh.read()

    assert "## [Unreleased]" in text


# ── The banner ───────────────────────────────────────────────────────────────

def test_the_banner_shows_the_version_between_the_name_and_the_status():
    """The header reads: name, version, status — in that order."""
    from core import __main__ as entry
    from core import ui

    banner = plain(ui.banner("Termux-AI", "Terminal AI ready.",
                              version=entry.VERSION))
    lines = [line for line in banner.splitlines() if line.strip()]

    assert lines[0].strip() == "▌ Termux-AI"
    assert lines[1].strip() == f"v{entry.VERSION}"
    assert lines[2].strip() == "Terminal AI ready."


def test_the_banner_without_a_version_still_works():
    """The other banners (sessions, plugins) pass no version."""
    from core import ui

    banner = plain(ui.banner("Plugins", "3 registered"))
    assert "Plugins" in banner
    assert "3 registered" in banner
    assert "v" not in banner.splitlines()[1]


def test_the_banner_does_not_double_the_v():
    """``v3.3.1`` and ``3.3.1`` both render as ``v3.3.1``."""
    from core import __main__ as entry
    from core import ui

    version = entry.VERSION
    assert f"v{version}" in plain(ui.banner("X", version=version))
    assert f"v{version}" in plain(ui.banner("X", version=f"v{version}"))
    assert "vv" not in plain(ui.banner("X", version=f"v{version}"))


# ── The entry point ──────────────────────────────────────────────────────────

def test_importing_the_entry_point_starts_nothing():
    """``core/__main__.py`` can be imported for the constant alone.

    The chat loop and the Termux property write sit behind the
    ``__main__`` guard, so reading the version never wakes a session.
    """
    result = subprocess.run(
        [sys.executable, "-c",
         "import core.__main__ as e; print(e.VERSION)"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "3.3.1"
    # Nothing was printed by the import itself — no banner, no chat loop.
    assert "Terminal AI ready" not in result.stdout


def test_the_entry_point_still_starts_the_chat_loop():
    """The guard must not stop ``python core`` from working."""
    source = open(os.path.join(REPO_ROOT, "core", "__main__.py"),
                  encoding="utf-8").read()

    assert 'if __name__ == "__main__":' in source
    assert "chat_loop()" in source
    assert "ensure_termux_property()" in source


# ── The real startup ─────────────────────────────────────────────────────────

def test_the_startup_header_shows_the_version(capsys, tmp_path, monkeypatch):
    """Run the actual chat loop and look at the header it prints."""
    from core import interface
    from core import __main__ as entry

    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "tts_enabled": False,
        "autonomous": False,
        "notify": False,
        "show_details": False,
        "stt_path": "",
    }), encoding="utf-8")
    monkeypatch.setattr(interface, "CONFIG_PATH", str(config_file))
    monkeypatch.setattr(interface, "HAS_STT", False)
    monkeypatch.setattr(interface, "_start_whatsapp_runtime", lambda: False)
    monkeypatch.setattr(interface, "_notify_plugin_setups", lambda: None)
    monkeypatch.setattr(paths, "LOG_FILE", str(tmp_path / "log.txt"))
    runtime_log.reset_state()

    def no_input(_prompt):
        raise EOFError

    monkeypatch.setattr(interface, "get_interactive_input", no_input)

    try:
        interface.chat_loop()
    finally:
        runtime_log.reset_state()

    out = capsys.readouterr().out
    assert f"v{entry.VERSION}" in out

    # The version sits between the name and the status line.
    name_at = out.index("Termux-AI")
    version_at = out.index(f"v{entry.VERSION}")
    ready_at = out.index("Terminal AI ready.")
    assert name_at < version_at < ready_at
