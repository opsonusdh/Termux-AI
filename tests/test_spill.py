"""Oversized tool results go to disk — and a spill file can be read back.

The two rules that make spilling safe are tested here explicitly:

* a read of a spill file is never spilled again (otherwise the model would
  follow a pointer to another pointer and never see what it asked for);
* a failed write leaves the original result inline (the spill is an
  optimisation; it must never turn a working tool call into a lost one).
"""
import json
import os
import stat
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "core") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "core"))

import paths  # noqa: E402
from core import spill  # noqa: E402


@pytest.fixture
def spill_home(tmp_path, monkeypatch):
    """Point the spill directory and the config at a temporary place."""
    monkeypatch.setattr(paths, "SPILL_DIR", str(tmp_path / "spill"))
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"spill_trigger_tokens": 100}), encoding="utf-8"
    )
    monkeypatch.setattr(paths, "CONFIG_FILE", str(config))
    monkeypatch.setattr(spill, "_trigger_cache", None)
    monkeypatch.setattr(spill, "_session_label", "test-session")
    return tmp_path


def _big(marker: str = "x", size: int = 5000) -> str:
    return (marker * size)


# ── the trigger ───────────────────────────────────────────────────────────────

def test_a_small_result_is_returned_unchanged(spill_home):
    text = "a short command output"
    assert spill.spill_result("shell", text, {"bash": "ls"}) == text
    assert not os.path.exists(paths.SPILL_DIR)


def test_a_big_result_is_spilled(spill_home):
    text = _big()
    out = spill.spill_result("browse_web", text, {"url": "https://example.com"})

    assert out != text
    assert "[SPILLED]" in out
    assert len(out) < len(text)
    # The beginning and the end survive: they are the part a reader needs.
    assert out.startswith("x")
    assert out.rstrip().endswith("x")


def test_the_full_text_is_on_disk(spill_home):
    text = _big("y", 5000)
    out = spill.spill_result("browse_web", text, {"url": "https://example.com"})

    path = _path_from_notice(out)
    assert os.path.isfile(path)
    with open(path, "r", encoding="utf-8") as handle:
        assert handle.read() == text


def test_the_notice_names_the_file_and_how_to_read_it(spill_home):
    out = spill.spill_result("browse_web", _big(), {"url": "https://x"})
    assert "read_file(path=" in out
    assert "search_in_files(path=" in out
    assert "Full output:" in out
    assert ".txt" in out


def test_the_spill_file_is_private(spill_home):
    out = spill.spill_result("shell", _big(), {"bash": "cat big"})
    path = _path_from_notice(out)

    file_mode = stat.S_IMODE(os.stat(path).st_mode)
    dir_mode = stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)
    assert file_mode == 0o600
    assert dir_mode == 0o700


# ── the exemption: a read of a spill file is never spilled ────────────────────

def test_reading_a_spill_file_is_exempt(spill_home):
    out = spill.spill_result("browse_web", _big(), {"url": "https://x"})
    path = _path_from_notice(out)
    relative = os.path.relpath(path, REPO_ROOT)

    assert spill.reads_spill_dir("read_file", {"path": relative})
    assert not spill.should_spill("read_file", {"path": relative}, _big("z", 9000))

    # And the call really does come through untouched.
    content = _big("z", 9000)
    assert spill.spill_result("read_file", content, {"path": relative}) == content


def test_searching_inside_the_spill_directory_is_exempt(spill_home):
    out = spill.spill_result("browse_web", _big(), {"url": "https://x"})
    path = _path_from_notice(out)
    directory = os.path.relpath(os.path.dirname(path), REPO_ROOT)

    assert spill.reads_spill_dir("search_in_files", {"path": directory})
    assert not spill.should_spill(
        "search_in_files", {"path": directory}, _big("z", 9000)
    )


def test_a_read_outside_the_spill_directory_is_not_exempt(spill_home):
    assert not spill.reads_spill_dir("read_file", {"path": "core/tools.py"})
    assert not spill.reads_spill_dir("read_file", {"path": ""})


def test_an_absolute_spill_path_is_exempt(spill_home):
    out = spill.spill_result("browse_web", _big(), {"url": "https://x"})
    path = _path_from_notice(out)
    assert spill.reads_spill_dir("read_file", {"path": path})


# ── the failure path: the result is never lost ───────────────────────────────

def test_a_failed_write_leaves_the_result_inline(spill_home, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(spill, "_write_spill_file", _boom)
    text = _big()
    assert spill.spill_result("browse_web", text, {"url": "https://x"}) == text


def test_spilling_can_be_switched_off(spill_home, monkeypatch):
    config = spill_home / "config.json"
    config.write_text(
        json.dumps({"spill_trigger_tokens": 100, "spill_enabled": False}),
        encoding="utf-8",
    )
    monkeypatch.setattr(spill, "_trigger_cache", None)

    text = _big()
    assert spill.spill_result("browse_web", text, {"url": "https://x"}) == text


def test_the_threshold_follows_the_config(spill_home, monkeypatch):
    config = spill_home / "config.json"
    config.write_text(
        json.dumps({"spill_trigger_tokens": 100000}), encoding="utf-8"
    )
    monkeypatch.setattr(spill, "_trigger_cache", None)

    text = _big()
    assert spill.spill_result("browse_web", text, {"url": "https://x"}) == text


# ── helpers ───────────────────────────────────────────────────────────────────

def _path_from_notice(out: str) -> str:
    """The absolute path of the spill file a notice names."""
    for line in out.splitlines():
        if line.startswith("Full output: "):
            relative = line[len("Full output: "):].strip()
            return os.path.join(REPO_ROOT, relative)
    raise AssertionError("no spill notice in:\n" + out[:500])
