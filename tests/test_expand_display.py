"""Tests for the expanded (/e) display of tool calls.

The expanded view is the "show me everything" mode.  Three things used to
break it:

* ``find_replace`` silently dropped its ``case_sensitive`` flag, so a
  replacement that matched differently than expected looked inexplicable;
* a tool call that was *refused* (plugin disabled, unknown tool) printed
  nothing at all — the model tried something and the screen stayed blank;
* the delegated sub-AI's reasoning was hidden whenever the provider reported
  it as ``reasoning_details`` (OpenRouter) instead of plain ``reasoning``.

These tests pin all three shut, plus the general rule that *every*
advertised tool shows *every* argument when the details are expanded.
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
from core import tools as tool_runtime  # noqa: E402
from core.tools import TOOLS_DESCRIPTION  # noqa: E402


def _sample_value(schema: dict, key: str):
    """A representative, non-empty value for one JSON-schema property."""
    stype = schema.get("type")
    if isinstance(stype, list):
        stype = next((t for t in stype if t != "null"), "string")
    if stype == "boolean":
        return False
    if stype == "integer":
        return 7
    if stype == "number":
        return 1.5
    if stype == "array":
        item = schema.get("items", {})
        if item.get("type") == "object":
            return [{"alpha": "one"}]
        return ["alpha", "beta"]
    if stype == "object":
        return {"alpha": "one"}
    if schema.get("enum"):
        return schema["enum"][0]
    return f"VALUE-{key}"


def _leaves(value):
    """Every scalar inside a structured value."""
    out = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        else:
            out.append(str(item))
    return out


def _args_for(tool: dict) -> dict:
    props = tool.get("function", {}).get("parameters", {}).get("properties", {})
    return {key: _sample_value(schema, key) for key, schema in props.items()}


# ── find_replace ──────────────────────────────────────────────────────────────

def test_find_replace_shows_the_case_sensitivity_flag():
    """The flag decides *which* occurrences are replaced — leaving it out
    made a surprising replacement look inexplicable."""
    header = llm_client._tool_header_lines(
        "find_replace",
        {"path": "a.py", "find": "x", "replace": "y", "case_sensitive": False},
        expanded=True,
    )
    assert "case-sensitive: False" in "\n".join(header["lines"])


def test_find_replace_shows_the_default_case_sensitivity():
    header = llm_client._tool_header_lines(
        "find_replace",
        {"path": "a.py", "find": "x", "replace": "y"},
        expanded=True,
    )
    assert "case-sensitive: True" in "\n".join(header["lines"])


def test_find_replace_understands_a_string_flag():
    """Models sometimes send booleans as strings."""
    header = llm_client._tool_header_lines(
        "find_replace",
        {"path": "a.py", "find": "x", "replace": "y", "case_sensitive": "false"},
        expanded=True,
    )
    assert "case-sensitive: False" in "\n".join(header["lines"])


# ── every tool shows every argument ───────────────────────────────────────────

def test_every_advertised_tool_shows_every_argument_when_expanded():
    """The general rule: no argument of any tool may be invisible."""
    offenders = []
    for tool in TOOLS_DESCRIPTION:
        name = tool.get("function", {}).get("name")
        args = _args_for(tool)
        if not args:
            continue
        header = llm_client._tool_header_lines(name, args, True)
        rendered = "\n".join(header["lines"])
        for key, value in args.items():
            if value in (None, "", [], {}):
                continue
            for probe in _leaves(value):
                if probe not in rendered:
                    offenders.append(f"{name}.{key}={probe!r}")
    assert not offenders, "invisible arguments: " + ", ".join(offenders)


def test_the_collapsed_view_stays_compact():
    """Collapsed mode is a preview — it must not grow into a dump."""
    for tool in TOOLS_DESCRIPTION:
        name = tool.get("function", {}).get("name")
        args = _args_for(tool)
        header = llm_client._tool_header_lines(name, args, False)
        assert header["lines"], name
        assert len(header["lines"]) <= 2, name


# ── refused calls are still visible ───────────────────────────────────────────

@pytest.fixture
def expanded_mode():
    """Turn the expanded display on without touching config.json."""
    from core import display_state
    previous = display_state.is_expanded()
    display_state.set_expanded(True, persist=False)
    yield
    display_state.set_expanded(previous, persist=False)


def test_a_refused_plugin_tool_call_is_shown(monkeypatch, expanded_mode):
    """A call the runtime refuses used to vanish from the screen entirely:
    the user could not tell what the model had tried, or why it had been
    turned down."""
    monkeypatch.setattr(
        llm_client.plugin_manager, "is_tool_allowed", lambda name: False
    )
    monkeypatch.setattr(
        llm_client.plugin_manager, "get_tool_plugin", lambda name: "gmail"
    )
    monkeypatch.setattr(
        llm_client.plugin_manager, "plugin_state",
        lambda name: {"enabled": False, "authorized": False},
    )

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        result = llm_client._dispatch_tool({
            "function": {
                "name": "send_gmail_message",
                "arguments": json.dumps({
                    "to": "someone@example.com",
                    "body": "hello",
                }),
            }
        })

    printed = buffer.getvalue()
    assert "SEND_GMAIL_MESSAGE" in printed
    assert "someone@example.com" in printed
    assert "not available" in printed
    assert result.startswith("[ERROR]")


def test_a_refused_plugin_tool_call_is_shown_when_collapsed(monkeypatch):
    """Even in the collapsed view the attempt has to be visible — the tag
    alone says what was tried."""
    from core import display_state

    monkeypatch.setattr(
        llm_client.plugin_manager, "is_tool_allowed", lambda name: False
    )
    monkeypatch.setattr(
        llm_client.plugin_manager, "get_tool_plugin", lambda name: "gmail"
    )
    monkeypatch.setattr(
        llm_client.plugin_manager, "plugin_state",
        lambda name: {"enabled": False, "authorized": False},
    )

    previous = display_state.is_expanded()
    display_state.set_expanded(False, persist=False)
    try:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            llm_client._dispatch_tool({
                "function": {
                    "name": "send_gmail_message",
                    "arguments": json.dumps({"to": "someone@example.com"}),
                }
            })
    finally:
        display_state.set_expanded(previous, persist=False)

    printed = buffer.getvalue()
    assert "SEND_GMAIL_MESSAGE" in printed
    assert "not available" in printed


def test_an_unknown_tool_call_shows_its_arguments(monkeypatch, expanded_mode):
    """The runtime cannot run it, but the user still sees what was asked."""
    monkeypatch.setattr(
        llm_client.plugin_manager, "is_tool_allowed", lambda name: True
    )

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        result = llm_client._dispatch_tool({
            "function": {
                "name": "no_such_tool",
                "arguments": json.dumps({"detail": "UNIQUE-ARG-VALUE"}),
            }
        })

    printed = buffer.getvalue()
    assert "UNIQUE-ARG-VALUE" in printed
    assert "Unknown tool" in printed
    assert result.startswith("[ERROR]")


# ── the delegated sub-AI's reasoning ──────────────────────────────────────────

def test_delegate_reasoning_reads_the_plain_field():
    assert tool_runtime._delegate_reasoning_text("thought it through", None) \
        == "thought it through"


def test_delegate_reasoning_reads_openrouter_reasoning_details():
    """OpenRouter leaves ``reasoning`` empty and reports the thinking as a
    list of typed blocks instead.  Reading only the plain field hid the
    sub-AI's thinking completely."""
    details = [
        {"type": "reasoning.text", "text": "first thought"},
        {"type": "reasoning.text", "text": "second thought"},
    ]
    text = tool_runtime._delegate_reasoning_text(None, details)
    assert "first thought" in text
    assert "second thought" in text


def test_delegate_reasoning_falls_back_to_the_summary_field():
    details = [{"type": "reasoning.summary", "summary": "a summary"}]
    assert "a summary" in tool_runtime._delegate_reasoning_text(None, details)


def test_delegate_reasoning_handles_nothing_at_all():
    assert tool_runtime._delegate_reasoning_text(None, None) == ""
    assert tool_runtime._delegate_reasoning_text("", []) == ""
