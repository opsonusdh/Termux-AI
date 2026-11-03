"""Tests for the real-time session log and the expanded display.

The log is the recovery mechanism: when a session is cut short, ``tail
logs/log.txt`` has to show what the runtime was doing at that moment.  These
tests pin the three properties that make that work:

* a write is visible on disk *immediately* (no buffering until end of turn),
* a tool call is recorded before the tool runs and its result right after,
* the file is capped, and the oldest lines are the ones that go.
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
from core import runtime_log


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    """Point the logger at a temporary file and reset its cached state."""
    target = tmp_path / "log.txt"
    monkeypatch.setattr(paths, "LOG_FILE", str(target))
    runtime_log.reset_state()
    yield str(target)
    runtime_log.reset_state()


# ── Real-time behaviour ─────────────────────────────────────────────────────

def test_write_is_visible_on_disk_immediately(log_file):
    """A write must be readable straight away — not buffered until later."""
    runtime_log.log_line("[test] first")

    with open(log_file, "r", encoding="utf-8") as fh:
        assert "first" in fh.read()


def test_event_body_is_indented_under_the_header(log_file):
    runtime_log.log_event("tool:call", "line one\nline two", header="shell")

    with open(log_file, "r", encoding="utf-8") as fh:
        text = fh.read()

    lines = text.splitlines()
    assert "[tool:call] shell" in lines[0]
    assert lines[1] == "    line one"
    assert lines[2] == "    line two"


def test_tool_call_and_result_are_both_recorded(log_file):
    runtime_log.log_tool_call("shell", {"bash": "echo hi"})
    runtime_log.log_tool_result("shell", "hi", 0.25)

    with open(log_file, "r", encoding="utf-8") as fh:
        text = fh.read()

    assert "[tool:call] shell" in text
    assert '"bash": "echo hi"' in text
    assert "[tool:result] shell (0.25s)" in text
    assert "hi" in text


def test_delegation_request_keeps_the_full_command(log_file):
    runtime_log.log_delegate_request(
        task="Audit the parser",
        context="The parser lives in core/shell_parse.py",
        intelligence="high",
        tools=["shell", "read_file"],
        iterations=5,
        system_prompt="You are a focused sub-assistant.",
    )

    with open(log_file, "r", encoding="utf-8") as fh:
        text = fh.read()

    assert "Audit the parser" in text
    assert "core/shell_parse.py" in text
    assert "intelligence=high" in text
    assert "shell, read_file" in text
    assert "You are a focused sub-assistant." in text


def test_delegate_tool_call_and_result_are_recorded(log_file):
    runtime_log.log_delegate_tool_call("read_file", {"path": "a.py"})
    runtime_log.log_delegate_tool_result("read_file", "1| import os")

    with open(log_file, "r", encoding="utf-8") as fh:
        text = fh.read()

    assert "[delegate:tool:call] read_file" in text
    assert '"path": "a.py"' in text
    assert "[delegate:tool:result] read_file" in text
    assert "1| import os" in text


# ── The cap ─────────────────────────────────────────────────────────────────

def test_log_is_capped_and_the_oldest_lines_are_dropped(log_file, monkeypatch):
    """Past the cap the oldest lines go — the newest ones stay.
    
    With strict line-by-line trimming, the file never exceeds MAX_LINES.
    """
    monkeypatch.setattr(runtime_log, "MAX_LINES", 100)

    # Write 101 lines - one past the cap
    for i in range(101):
        runtime_log.log_line(f"line-{i}")

    with open(log_file, "r", encoding="utf-8") as fh:
        lines = [l.rstrip("\n") for l in fh if l.strip()]

    # File should be at most MAX_LINES (100)
    assert len(lines) <= 100
    # The newest line survived
    assert lines[-1] == "line-100"
    # The oldest line was dropped
    assert "line-0" not in lines
    # Some recent lines survived
    assert "line-99" in lines


def test_the_file_never_stays_above_the_cap(log_file, monkeypatch):
    """The cap holds after *every* write, not just at the end of a burst."""
    monkeypatch.setattr(runtime_log, "MAX_LINES", 100)

    for i in range(150):
        runtime_log.log_line(f"line-{i}")
        count = sum(1 for _ in open(log_file, "r", encoding="utf-8"))
        assert count <= 100


def test_trim_never_leaves_the_file_above_the_cap(log_file, monkeypatch):
    monkeypatch.setattr(runtime_log, "MAX_LINES", 50)

    for i in range(500):
        runtime_log.log_line(f"x{i}")

    count = sum(1 for _ in open(log_file, "r", encoding="utf-8"))
    assert count <= 50


def test_trim_keeps_the_file_readable_by_a_tail(log_file, monkeypatch):
    """The trimmed file must still be a clean, newline-terminated text file."""
    monkeypatch.setattr(runtime_log, "MAX_LINES", 30)

    for i in range(120):
        runtime_log.log_line(f"row-{i}")

    with open(log_file, "r", encoding="utf-8") as fh:
        text = fh.read()

    assert text.endswith("\n")
    assert "row-119" in text


# ── Expanded display ────────────────────────────────────────────────────────

def _headers():
    from core import llm_client
    return llm_client


def test_expanded_header_shows_every_argument():
    """A tool whose arguments were not on the old preference list used to
    show nothing at all even with the details expanded."""
    llm = _headers()
    header = llm._tool_header_lines(
        "delegate_subtask",
        {
            "task": "Audit the parser",
            "context": "core/shell_parse.py",
            "intelligence": "high",
            "tool_call_iterations": 5,
        },
        expanded=True,
    )
    body = "\n".join(header["lines"])
    assert "task: Audit the parser" in body
    assert "context: core/shell_parse.py" in body
    assert "intelligence: high" in body
    assert "tool_call_iterations: 5" in body


def test_collapsed_header_stays_compact():
    llm = _headers()
    header = llm._tool_header_lines(
        "delegate_subtask",
        {"task": "Audit the parser", "context": "core/shell_parse.py"},
        expanded=False,
    )
    assert header["lines"] == ["[DELEGATE_SUBTASK]"]


def test_expanded_write_file_shows_the_content():
    llm = _headers()
    header = llm._tool_header_lines(
        "write_file",
        {"path": "a.py", "content": "print('x')\nprint('y')", "mode": "overwrite"},
        expanded=True,
    )
    body = "\n".join(header["lines"])
    assert "a.py" in body
    assert "print('x')" in body
    assert "print('y')" in body


def test_expanded_find_replace_shows_the_full_pattern():
    llm = _headers()
    long_pattern = "A" * 300
    header = llm._tool_header_lines(
        "find_replace",
        {"path": "a.py", "find": long_pattern, "replace": "B" * 300},
        expanded=True,
    )
    body = "\n".join(header["lines"])
    assert long_pattern in body
    assert "B" * 300 in body


def test_expanded_shell_shows_the_whole_command():
    llm = _headers()
    command = "echo one\necho two\necho three\necho four"
    header = llm._tool_header_lines(
        "shell", {"bash": command, "timeout": 30}, expanded=True
    )
    body = "\n".join(header["lines"])
    assert "echo four" in body
    assert "timeout=30" in body


# ── The dispatch path ───────────────────────────────────────────────────────

def test_dispatch_logs_the_call_before_and_the_result_after(log_file):
    """A tool call must be in the log before the tool runs, and its result
    right after — that is what makes the log useful after a crash."""
    from core import llm_client

    result = llm_client._dispatch_tool({
        "function": {
            "name": "get_datetime",
            "arguments": "{}",
        }
    })

    assert isinstance(result, str)
    with open(log_file, "r", encoding="utf-8") as fh:
        text = fh.read()

    assert "[tool:call] get_datetime" in text
    assert "[tool:result] get_datetime" in text
    # The call is recorded before the result.
    assert text.index("[tool:call] get_datetime") < text.index(
        "[tool:result] get_datetime"
    )
