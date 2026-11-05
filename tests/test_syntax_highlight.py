"""Tests for the syntax highlighting of tool calls and the permission prompt.

The markdown renderer already highlighted fenced code blocks with Pygments.
The same highlighter now colours the code the runtime shows while it works:

* the command a ``shell`` call runs — always **bash**;
* the content a ``write_file`` puts on disk — with the lexer of the *target
  file's* extension;
* the source a ``read_file`` brings back — with the lexer of *that file's*
  extension;
* the command the permission gate asks about — always **bash**.

Two details matter and are pinned here:

* the line numbers ``read_file`` prefixes its output with stay **out** of the
  lexer — fed to it, every line would start at column 0 and the indentation
  structure of the file would be lost;
* a highlighted snippet is never wrapped in the dim tool colour, because the
  dim attribute would dull the very colours the highlighter produced.
"""
import io
import json
import os
import sys
from contextlib import redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "core") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "core"))

from core import display_state  # noqa: E402
from core import llm_client  # noqa: E402
from core import renderer  # noqa: E402


def _pygments_available() -> bool:
    try:
        import pygments  # noqa: F401
        return True
    except ImportError:
        return False


def _plain(text: str) -> str:
    """The text without any ANSI colour codes."""
    return renderer.ANSI_ESCAPE.sub("", text)


@pytest.fixture
def expanded_mode():
    """Turn the expanded display on without touching config.json."""
    previous = display_state.is_expanded()
    display_state.set_expanded(True, persist=False)
    yield
    display_state.set_expanded(previous, persist=False)


# ── the language is decided by the file ───────────────────────────────────────

def test_the_extension_decides_the_language():
    assert renderer.language_for_path("core/renderer.py") == "python"
    assert renderer.language_for_path("/tmp/app.js") == "javascript"
    assert renderer.language_for_path("notes.md") == "markdown"
    assert renderer.language_for_path("run.sh") == "bash"


def test_extension_less_files_are_recognised_by_name():
    assert renderer.language_for_path("Makefile") == "makefile"
    assert renderer.language_for_path("Dockerfile") == "docker"


def test_an_unknown_file_has_no_language():
    """No guess: an unknown extension is rendered as plain text."""
    assert renderer.language_for_path("data/blob.qqq") == ""
    assert renderer.language_for_path("") == ""


# ── the snippet renderer ──────────────────────────────────────────────────────

def test_a_snippet_is_drawn_as_a_labelled_box():
    lines = renderer.render_code_snippet("def hi():\n    pass", "python")
    assert lines[0].startswith(renderer.GRAY + "╭──")
    assert "python" in _plain(lines[0])
    assert lines[-1].startswith(renderer.GRAY + "╰")
    assert "def hi():" in _plain(lines[1])


@pytest.mark.skipif(not _pygments_available(), reason="pygments is not installed")
def test_a_snippet_is_colourised():
    lines = renderer.render_code_snippet("def hi():\n    pass", "python")
    assert "\x1b[" in "".join(lines)


def test_an_empty_snippet_draws_nothing():
    assert renderer.render_code_snippet("", "python") == []
    assert renderer.render_code_snippet("   \n  ", "python") == []


# ── shell calls ───────────────────────────────────────────────────────────────

def test_a_shell_command_is_highlighted_as_bash():
    header = llm_client._tool_header_lines(
        "shell", {"bash": "ls -la && echo hello"}, expanded=True
    )
    _meta, lang, code = llm_client._tool_snippet("shell", {}, header)
    assert lang == "bash"
    assert code == "ls -la && echo hello"


def test_the_shell_command_is_shown_as_a_code_box():
    header = llm_client._tool_header_lines(
        "shell", {"bash": "ls -la && echo hello"}, expanded=True
    )
    parts = llm_client._tool_display_parts("shell", {}, header, expanded=True)
    rendered = "\n".join(text for text, _kind in parts)
    assert "ls -la" in _plain(rendered)
    assert "bash" in _plain(rendered)


def test_the_collapsed_preview_stays_short():
    """The collapsed view is a preview — it must not grow into a box."""
    header = llm_client._tool_header_lines(
        "shell", {"bash": "ls\nls -l\necho x\nsleep 1"}, expanded=False
    )
    parts = llm_client._tool_display_parts("shell", {}, header, expanded=False)
    assert len(parts) <= 5
    assert not any("╭──" in text for text, _kind in parts)


# ── writes ────────────────────────────────────────────────────────────────────

def test_the_written_content_uses_the_extension_of_the_target_file():
    header = llm_client._tool_header_lines(
        "write_file",
        {"path": "core/demo.py", "content": "def hi():\n    return 1"},
        expanded=True,
    )
    _meta, lang, code = llm_client._tool_snippet(
        "write_file", {"path": "core/demo.py"}, header
    )
    assert lang == "python"
    assert "def hi():" in code


def test_a_written_json_file_is_highlighted_as_json():
    header = llm_client._tool_header_lines(
        "write_file",
        {"path": "config/x.json", "content": '{"a": 1}'},
        expanded=True,
    )
    _meta, lang, _code = llm_client._tool_snippet(
        "write_file", {"path": "config/x.json"}, header
    )
    assert lang == "json"


# ── reads ─────────────────────────────────────────────────────────────────────

def test_the_read_output_uses_the_extension_of_the_file():
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        llm_client._print_tool_output(
            "read_file",
            {"path": "core/demo.py"},
            "1| def hi():\n2|     return 1",
            llm_client.DIM,
        )
    printed = buffer.getvalue()
    assert "python" in _plain(printed)
    assert "def hi():" in _plain(printed)


def test_the_line_numbers_stay_out_of_the_highlighted_code():
    """The prefix is metadata, not source — it must not reach the lexer."""
    lines = llm_client._highlight_numbered_code("1| def hi():\n2|     return 1", "python")
    assert len(lines) == 2
    assert lines[0].startswith("1| ")
    assert lines[1].startswith("2| ")
    # The number is plain metadata, the code behind it carries the colour.
    number, code = lines[0].split("| ", 1)
    assert number == "1"
    assert "\x1b[" in code


def test_a_read_error_is_not_highlighted():
    """An error message is not source code."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        llm_client._print_tool_output(
            "read_file", {"path": "nope.py"}, "[ERROR] File not found: nope.py", llm_client.DIM
        )
    printed = buffer.getvalue()
    assert "[ERROR] File not found: nope.py" in _plain(printed)
    assert "╭──" not in printed


# ── the permission gate ───────────────────────────────────────────────────────

def test_the_permission_command_is_highlighted_as_bash(monkeypatch, capsys):
    from core import permissions

    # The gate has to actually ask: autonomous mode would wave it through, and
    # the notification must not reach the user's phone from a test.
    monkeypatch.setattr(permissions, "is_autonomous", lambda: False)
    monkeypatch.setattr(permissions, "is_voice_available", lambda: False)
    monkeypatch.setattr(permissions.notifier, "permission", lambda requests: False)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")

    approved, _reason = permissions.validate_command("cd /tmp")

    assert approved is False
    printed = capsys.readouterr().out
    assert "cd /tmp" in _plain(printed)
    assert "bash" in _plain(printed)
    assert "\x1b[" in printed


def test_the_permission_command_is_still_readable_without_colour(monkeypatch, capsys):
    """Colour is decoration — the command itself must stay legible."""
    from core import permissions

    monkeypatch.setattr(permissions, "is_autonomous", lambda: False)
    monkeypatch.setattr(permissions, "is_voice_available", lambda: False)
    monkeypatch.setattr(permissions.notifier, "permission", lambda requests: False)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")

    permissions.validate_command("cd /tmp")

    printed = _plain(capsys.readouterr().out)
    assert "[PERMISSION] The AI wants to run:" in printed
    assert "cd /tmp" in printed
    assert "Reason:" in printed


# ── end to end through the tool dispatcher ────────────────────────────────────

def test_a_real_read_call_is_highlighted(expanded_mode):
    """The whole path: dispatch → tool → the file's source on the screen."""
    import paths

    target = os.path.join(paths.WORKSPACE_DIR, "highlight_demo.py")
    os.makedirs(paths.WORKSPACE_DIR, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("def hi():\n    return 1\n")

    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            llm_client._dispatch_tool({
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": target}),
                }
            })
    finally:
        try:
            os.remove(target)
        except OSError:
            pass

    printed = buffer.getvalue()
    assert "[READING FILE]" in printed
    assert "[OUTPUT]" in printed
    assert "def hi():" in _plain(printed)
    assert "python" in _plain(printed)
    assert "\x1b[" in printed
