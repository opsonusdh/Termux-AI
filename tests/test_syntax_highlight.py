"""Tests for the syntax highlighting of tool calls and the permission prompt.

The markdown renderer already highlighted fenced code blocks with Pygments.
The same highlighter now colours the code the runtime shows while it works:

* the command a ``shell`` call runs — always **bash**;
* the content a ``write_file`` puts on disk — with the lexer of the *target
  file's* extension;
* the find and replace patterns of a ``find_replace`` call — with the lexer of
  the *target file's* extension;
* the source a ``read_file`` brings back — with the lexer of *that file's*
  extension;
* the lines a ``find_replace`` call changed — with the lexer of *that file's*
  extension;
* the command the permission gate asks about — always **bash**.

Three details matter and are pinned here:

* the line numbers ``read_file`` prefixes its output with stay **out** of the
  lexer — fed to it, every line would start at column 0 and the indentation
  structure of the file would be lost;
* a highlighted snippet is never wrapped in the dim tool colour, because the
  dim attribute would dull the very colours the highlighter produced;
* the gutter frame (``╭── lang ──`` … ``╰──``) belongs to the **markdown**
  renderer alone.  The tool viewer shows the colour *without* the frame: the
  frame repeats itself on every single call, and it hides the very
  indentation the highlighter is trying to show.
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

from core import llm_client  # noqa: E402
from core import renderer  # noqa: E402
from core import tools as tool_runtime  # noqa: E402


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
def expanded_mode(display_expanded):
    """Turn the expanded display on without touching config.json.

    The preference lives in the user's ``config.json`` and is re-read on
    every call, so it is pinned by the shared ``display_expanded`` fixture
    rather than set in memory — an in-memory flag is overwritten by the
    file the moment it is read.
    """


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
    """The markdown path keeps its gutter frame — the frame is what tells a
    reader where a fenced code block starts and ends."""
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


def test_the_tool_viewer_code_carries_no_frame():
    """The tool viewer gets the colour, not the markdown renderer's box.

    A frame around every command and every file listing repeats itself on
    every call — and it hides the indentation the highlighter is there to
    show.
    """
    lines = renderer.render_tool_code("def hi():\n    pass", "python")
    assert "def hi():" in _plain("\n".join(lines))
    assert not any("╭" in line or "│" in line or "╰" in line for line in lines)


def test_the_tool_viewer_code_draws_nothing_for_empty_input():
    assert renderer.render_tool_code("", "python") == []
    assert renderer.render_tool_code("   \n  ", "python") == []


# ── shell calls ───────────────────────────────────────────────────────────────

def test_a_shell_command_is_highlighted_as_bash():
    header = llm_client._tool_header_lines(
        "shell", {"bash": "ls -la && echo hello"}, expanded=True
    )
    _meta, lang, code = llm_client._tool_snippet("shell", {}, header)
    assert lang == "bash"
    assert code == "ls -la && echo hello"


def test_the_shell_command_is_colourised_but_not_framed():
    header = llm_client._tool_header_lines(
        "shell", {"bash": "ls -la && echo hello"}, expanded=True
    )
    parts = llm_client._tool_display_parts("shell", {}, header, expanded=True)
    rendered = "\n".join(text for text, _kind in parts)
    assert "ls -la" in _plain(rendered)
    assert "\x1b[" in rendered
    assert "╭──" not in rendered
    assert "╰" not in rendered


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


# ── find and replace ──────────────────────────────────────────────────────────

def test_the_find_and_replace_patterns_use_the_extension_of_the_target_file():
    """The patterns are code of the file they belong to — so they are
    coloured with that file's lexer."""
    header = llm_client._tool_header_lines(
        "find_replace",
        {"path": "core/demo.py", "find": "old = 1", "replace": "new = 2"},
        expanded=True,
    )
    parts = llm_client._tool_display_parts(
        "find_replace", {"path": "core/demo.py"}, header, expanded=True
    )
    rendered = "\n".join(text for text, _kind in parts)
    assert "old = 1" in _plain(rendered)
    assert "new = 2" in _plain(rendered)
    assert "\x1b[" in rendered
    assert "╭──" not in rendered


def test_the_find_replace_patterns_keep_their_labels():
    header = llm_client._tool_header_lines(
        "find_replace",
        {"path": "core/demo.py", "find": "old = 1", "replace": "new = 2"},
        expanded=True,
    )
    parts = llm_client._tool_display_parts(
        "find_replace", {"path": "core/demo.py"}, header, expanded=True
    )
    rendered = "\n".join(text for text, _kind in parts)
    assert "find:" in _plain(rendered)
    assert "replace:" in _plain(rendered)


def test_a_find_replace_reports_the_lines_it_changed(tmp_path):
    """The tool says which lines it touched — that is what lets the runtime
    show them in colour instead of only saying that something happened."""
    from unittest.mock import patch

    target = tmp_path / "demo.py"
    target.write_text("a = 1\nb = 2\na = 3\n", encoding="utf-8")

    with patch("core.tools.request_file_permission", return_value=(True, "OK")):
        result = tool_runtime.find_replace(str(target), "a = ", "c = ")

    assert "at lines 1, 3" in result
    assert target.read_text(encoding="utf-8") == "c = 1\nb = 2\nc = 3\n"


def test_a_find_replace_result_shows_the_changed_lines_in_colour(tmp_path):
    target = tmp_path / "demo.py"
    target.write_text("def hi():\n    return old\n", encoding="utf-8")

    output = "Successfully replaced 1 occurrence in demo.py (at lines 2)."
    lines = llm_client._find_replace_changed_lines({"path": str(target)}, output)

    rendered = "\n".join(lines)
    assert "2| " in rendered
    assert "return old" in _plain(rendered)
    assert "\x1b[" in rendered


def test_a_find_replace_without_line_numbers_shows_no_preview(tmp_path):
    """A report that carries no line numbers means no preview — the status
    line above it is still the truth."""
    target = tmp_path / "demo.py"
    target.write_text("a = 1\n", encoding="utf-8")
    assert llm_client._find_replace_changed_lines(
        {"path": str(target)},
        "No occurrences of the search text found in demo.py.",
    ) == []


# ── reads ─────────────────────────────────────────────────────────────────────

def test_the_read_output_is_highlighted_with_the_extension_of_the_file():
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        llm_client._print_tool_output(
            "read_file",
            {"path": "core/demo.py"},
            "1| def hi():\n2|     return 1",
            llm_client.DIM,
        )
    printed = buffer.getvalue()
    assert "def hi():" in _plain(printed)
    # The colour comes from the file's own lexer …
    assert "\x1b[" in printed
    # … the gutter frame does not — it belongs to the markdown renderer.
    assert "╭──" not in printed
    assert "╰" not in printed


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
    assert "\x1b[" in printed
    # The gutter frame is the markdown renderer's, not the gate's.
    assert "╭──" not in printed


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
    assert "\x1b[" in printed
    assert "╭──" not in printed
