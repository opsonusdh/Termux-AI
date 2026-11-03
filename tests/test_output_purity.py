"""Regression tests: the model's working trace must not leak into the answer.

Bug (reported from workspace/example.txt): the reply the interface prints
after "AI >" / "Agent AI >" started with a paragraph of the model's own
running commentary — "Let me look at the actual fallback extraction code
first.Let me read the actual fallback extraction code…" — glued to the
answer without a line break.

Cause: `_stitch_assistant_turns()` concatenated *every* assistant message
of the turn, including the narration a model writes next to its tool calls,
into the final reply.  The narration is now separated from the answer and
printed in the trace channel instead.

These tests drive the real request path (`_ask_with_slots` with a fake
provider), not just the helper.
"""
import io
import json
import sys
from contextlib import redirect_stdout

import pytest

REPO_ROOT = __import__("os").path.dirname(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__))
)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if REPO_ROOT + "/core" not in sys.path:
    sys.path.insert(0, REPO_ROOT + "/core")

import core.llm_client as lc


NARRATION = [
    "Let me look at the actual fallback extraction code first.",
    "Let me read the actual fallback extraction code and the browser path side by side.",
    "Now I see the fallback extraction. It strips `script/style/nav/footer/header` "
    "but has no visibility filtering at all.",
]
ANSWER = (
    "Everything green — the previous session's e2e suite still passes too. "
    "Here's where it landed.\n\n"
    "First: your premise was inverted, and I should have said so sooner."
)


# ── fake provider ────────────────────────────────────────────────────────────

class _FakeChoice:
    def __init__(self, message, finish_reason):
        self.message = message
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, payload):
        self.choices = [_FakeChoice(
            payload["message"], payload.get("finish_reason", "stop"))]


class _FakeRaw:
    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps({"choices": [{
            "message": payload["message"],
            "finish_reason": payload.get("finish_reason", "stop"),
        }]})

    def parse(self):
        return _FakeResponse(self._payload)


class _FakeCompletions:
    def __init__(self, payloads):
        self._payloads = payloads
        self._index = 0
        self.with_raw_response = self

    def create(self, **kwargs):
        payload = self._payloads[self._index]
        self._index += 1
        return _FakeRaw(payload)


class _FakeClient:
    def __init__(self, payloads):
        self.chat = type("chat", (), {})()
        self.chat.completions = _FakeCompletions(payloads)


def _tool_call_turn(text, call_id="call_0"):
    """An assistant message that carries narration *and* a tool call."""
    return {
        "message": {
            "role": "assistant",
            "content": text,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
        "finish_reason": "tool_calls",
    }


def _final_turn(text):
    return {"message": {"role": "assistant", "content": text},
            "finish_reason": "stop"}


def _run_turn(monkeypatch, payloads, tool_result="tool output",
              expanded=False):
    """Run one `_ask_with_slots` turn against the fake provider."""
    client = _FakeClient(payloads)

    monkeypatch.setattr(lc, "_make_client", lambda pid, api_key: client)
    monkeypatch.setattr(lc, "API_KEYS", {"testprov": ["key-1"]})
    # The tool itself is irrelevant here — only the assistant messages
    # around it matter.
    monkeypatch.setattr(lc, "_dispatch_tool",
                        lambda tc, voice=False: tool_result)
    # The expand/collapse preference is global state (config.json), so it is
    # pinned here — otherwise the test depends on the order the suite runs.
    monkeypatch.setattr(lc.display_state, "is_expanded", lambda: expanded)

    slots = [{
        "provider_id": "testprov",
        "name": "fake-model",
        "max_tokens": 1024,
    }]

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        reply = lc._ask_with_slots(
            "do the thing", [], False, "system prompt", slots
        )
    return reply, buffer.getvalue()


# ── tests ────────────────────────────────────────────────────────────────────

def test_tool_call_narration_is_not_part_of_the_answer(monkeypatch):
    """The narration next to a tool call must not be stitched into the reply."""
    payloads = [_tool_call_turn(NARRATION[0], "c0")]
    payloads += [_tool_call_turn(n, f"c{i}") for i, n in
                 enumerate(NARRATION[1:], start=1)]
    payloads.append(_final_turn(ANSWER))

    reply, printed = _run_turn(monkeypatch, payloads)

    for fragment in NARRATION:
        assert fragment not in reply, (
            f"narration leaked into the answer: {fragment!r}"
        )
    assert ANSWER in reply
    assert reply.strip().startswith("Everything green")
    # The trace is still accounted for — in its own labelled block, not in
    # the answer.  (Collapsed by default, so only the summary is printed.)
    assert "[Working trace]" in printed
    assert "working trace collapsed:" in printed
    assert NARRATION[0] not in reply


def test_answer_is_printed_without_the_trace(monkeypatch):
    """What the interface prints as the answer block must be trace-free."""
    from core.renderer import render_markdown_terminal

    payloads = [_tool_call_turn(NARRATION[0], "c0"),
                _final_turn(ANSWER)]
    reply, printed = _run_turn(monkeypatch, payloads)

    rendered = render_markdown_terminal(reply)
    assert NARRATION[0] not in rendered
    assert "First: your premise was inverted" in rendered
    # The narration is printed, but only as its own block, not glued to the
    # first paragraph of the answer.
    assert "Let me look at the actual fallback extraction code first.Let me" not in printed


def test_trace_is_collapsed_by_default(monkeypatch):
    """Collapsed mode must not dump the trace into the terminal."""
    payloads = [_tool_call_turn(NARRATION[0], "c0"),
                _final_turn(ANSWER)]
    reply, printed = _run_turn(monkeypatch, payloads, expanded=False)

    assert "[Working trace]" in printed
    assert "working trace collapsed:" in printed
    assert NARRATION[0] not in printed


def test_trace_is_shown_when_expanded(monkeypatch):
    """Expanded mode (Ctrl+O) reveals the trace."""
    payloads = [_tool_call_turn(NARRATION[0], "c0"),
                _final_turn(ANSWER)]
    reply, printed = _run_turn(monkeypatch, payloads, expanded=True)

    assert "[Working trace]" in printed
    assert NARRATION[0] in printed
    assert NARRATION[0] not in reply


def test_length_continuation_stays_in_the_answer(monkeypatch):
    """A length-truncated answer and its continuation are one answer."""
    payloads = [
        {"message": {"role": "assistant", "content": "The answer starts"},
         "finish_reason": "length"},
        _final_turn(" here and continues."),
    ]
    reply, printed = _run_turn(monkeypatch, payloads)

    assert "The answer starts" in reply
    assert "here and continues." in reply
    assert "Working trace" not in printed


def test_split_helper_separates_narration_from_answer():
    messages = [
        {"role": "assistant", "content": NARRATION[0],
         "tool_calls": [{"id": "c0", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c0", "content": "output"},
        {"role": "assistant", "content": NARRATION[1],
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}]},
    ]
    answer, trace = lc._split_assistant_turns(messages, ANSWER)

    assert answer == ANSWER
    assert NARRATION[0] in trace and NARRATION[1] in trace
    assert NARRATION[0] not in answer


def test_narration_only_turn_is_still_answered():
    """No final answer at all: the narration is used instead of a bare
    "[EMPTY RESPONSE]"."""
    messages = [
        {"role": "assistant", "content": NARRATION[0],
         "tool_calls": [{"id": "c0", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}]},
    ]
    answer, trace = lc._split_assistant_turns(messages, "")
    assert answer == ""
    assert trace == NARRATION[0]
