"""The runtime context block: the permission line and the skill catalogue.

The model used to learn what it was allowed to do by hitting the permission
gate — every protected path and every outside-the-workspace command cost a
turn and produced a refusal.  One line in the prompt says the same thing
for free, and because the block is rebuilt for every request, a mode toggle
takes effect on the next turn.
"""
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "core") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "core"))

from core import llm_client, permissions, tools  # noqa: E402


# ── the permission line ───────────────────────────────────────────────────────

def test_the_line_says_the_gate_is_active(monkeypatch):
    monkeypatch.setattr(permissions, "is_autonomous", lambda: False)
    line = permissions.permission_snapshot()
    assert "Autonomous mode is OFF" in line
    assert "permission gate is active" in line


def test_the_line_says_the_gate_is_bypassed(monkeypatch):
    monkeypatch.setattr(permissions, "is_autonomous", lambda: True)
    line = permissions.permission_snapshot()
    assert "Autonomous mode is ON" in line
    assert "bypassed" in line


def test_the_line_is_one_line(monkeypatch):
    monkeypatch.setattr(permissions, "is_autonomous", lambda: False)
    assert "\n" not in permissions.permission_snapshot()


# ── the block that goes into the prompt ───────────────────────────────────────

def test_the_block_carries_the_permission_line_and_the_core_skills(monkeypatch):
    monkeypatch.setattr(permissions, "is_autonomous", lambda: False)
    block = tools.runtime_context_block()
    assert "[RUNTIME CONTEXT]" in block
    assert "[PERMISSIONS]" in block
    assert "- coding —" in block
    assert "- verification —" in block


def test_the_block_does_not_list_the_imported_skills(monkeypatch):
    block = tools.runtime_context_block()
    assert "doc-coauthoring" not in block
    assert "frontend-design" not in block
    assert "skill(\"\")" in block


# ── the prompt actually carries it ────────────────────────────────────────────

@pytest.mark.parametrize("caller", ["ask_ai", "ask_agent"])
def test_every_request_carries_the_runtime_context(caller, monkeypatch):
    captured = {}

    def _capture(prompt, history, voice, system_content, model_slots):
        captured["system"] = system_content
        return "ok"

    monkeypatch.setattr(llm_client, "_ask_with_slots", _capture)
    getattr(llm_client, caller)("hello")

    assert "[RUNTIME CONTEXT]" in captured["system"]
    assert "[PERMISSIONS]" in captured["system"]
    assert "- coding —" in captured["system"]


# ── the skill tool is dispatchable ────────────────────────────────────────────

def test_the_skill_tool_returns_the_manual():
    result = llm_client._dispatch_tool({
        "function": {
            "name": "skill",
            "arguments": json.dumps({"name": "coding"}),
        }
    })
    assert isinstance(result, str)
    assert "Coding Standards" in result
    assert not result.lstrip().startswith("---")


def test_the_skill_tool_lists_the_catalogue():
    result = llm_client._dispatch_tool({
        "function": {"name": "skill", "arguments": json.dumps({"name": ""})}
    })
    assert "Core skills:" in result
    assert "Imported skills:" in result
