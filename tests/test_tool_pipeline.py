"""The loop guard's vocabulary: what counts as a failure, what counts as
the same call.

Two things are tested here, and both were wrong before:

* the failure check lived inline in the tool loop, so a tool that returned
  legitimate data starting with ``[ERROR]`` was counted as a failure and a
  real failure without one of the known prefixes was not counted at all —
  it is now one tested helper;
* the call signature was the raw argument string, so ``{"a":1,"b":2}`` and
  ``{"b":2,"a":1}`` were two different calls and the guard never saw the
  repetition it exists to catch.
"""
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "core") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "core"))

from core import tool_pipeline  # noqa: E402


# ── what counts as a failure ──────────────────────────────────────────────────

@pytest.mark.parametrize("marker,kind", [
    ("[ERROR] File not found", "error"),
    ("[TOOL ERROR] read_file raised", "tool_error"),
    ("[PERMISSION DENIED] 'core/x'", "permission_denied"),
    ("[TIMEOUT] after 30s", "timeout"),
])
def test_the_known_markers_are_recognised(marker, kind):
    assert tool_pipeline.looks_like_error(marker) == kind


def test_ordinary_output_is_not_a_failure():
    assert tool_pipeline.looks_like_error("total 42\ndone") is None
    assert tool_pipeline.looks_like_error("") is None


def test_a_marker_in_the_middle_is_not_a_failure():
    """The check is strict about position — as it always was."""
    assert tool_pipeline.looks_like_error("output:\n[ERROR] something") is None


def test_a_non_string_is_not_a_failure():
    assert tool_pipeline.looks_like_error(None) is None
    assert tool_pipeline.looks_like_error({"error": "x"}) is None
    assert tool_pipeline.looks_like_error(42) is None


def test_a_route_can_report_its_own_failure_kind():
    error = tool_pipeline.ToolError("timeout", "the call took too long")
    assert tool_pipeline.looks_like_error(error) == "timeout"
    assert str(error) == "the call took too long"


# ── what counts as the same call ──────────────────────────────────────────────

def test_the_argument_order_does_not_matter():
    first = tool_pipeline.normalize_call_signature(
        "read_file", {"path": "core/tools.py", "segment_start": 1}
    )
    second = tool_pipeline.normalize_call_signature(
        "read_file", {"segment_start": 1, "path": "core/tools.py"}
    )
    assert first == second


def test_the_signature_carries_the_tool_name():
    first = tool_pipeline.normalize_call_signature("read_file", {"path": "a"})
    second = tool_pipeline.normalize_call_signature("write_file", {"path": "a"})
    assert first != second


def test_different_arguments_are_different_calls():
    first = tool_pipeline.normalize_call_signature("read_file", {"path": "a"})
    second = tool_pipeline.normalize_call_signature("read_file", {"path": "b"})
    assert first != second


def test_nested_argument_order_does_not_matter():
    first = tool_pipeline.normalize_call_signature(
        "shell", {"bash": "ls", "env": {"A": 1, "B": 2}}
    )
    second = tool_pipeline.normalize_call_signature(
        "shell", {"env": {"B": 2, "A": 1}, "bash": "ls"}
    )
    assert first == second


def test_raw_json_arguments_are_accepted():
    from_dict = tool_pipeline.normalize_call_signature("read_file", {"path": "a"})
    from_json = tool_pipeline.normalize_call_signature(
        "read_file", '{"path": "a"}'
    )
    assert from_dict == from_json


def test_malformed_arguments_are_still_compared():
    first = tool_pipeline.normalize_call_signature("read_file", "{not json")
    second = tool_pipeline.normalize_call_signature("read_file", "{not json")
    third = tool_pipeline.normalize_call_signature("read_file", "{other json")
    assert first == second
    assert first != third


def test_the_signature_is_a_string():
    signature = tool_pipeline.normalize_call_signature("shell", {"bash": "ls"})
    assert isinstance(signature, str)
    assert signature.startswith("shell:")
