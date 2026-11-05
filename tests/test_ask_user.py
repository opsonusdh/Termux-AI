"""Tests for the ask_user tool: numbered options *and* free-form answers.

The tool has two ways for the user to answer: pick an option by number, or
write the answer the AI should get.  The prompt has to say so, and a typed
answer has to reach the AI exactly as written — that is the whole point when
none of the offered options fit.
"""
import builtins
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core import tools  # noqa: E402


class FakeInput:
    """Stand-in for ``builtins.input`` that records prompts and replays answers."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


@pytest.fixture
def fake_input(monkeypatch):
    """Install a fake ``input()`` and hand it back for inspection."""

    def _install(*answers):
        fake = FakeInput(*answers)
        monkeypatch.setattr(builtins, "input", fake)
        return fake

    return _install


@pytest.fixture(autouse=True)
def _silent_notifier(monkeypatch):
    """Never push a system notification from a unit test."""
    monkeypatch.setattr(tools.notifier, "question", lambda *a, **k: False)


# ── The prompt ───────────────────────────────────────────────────────────────

def test_prompt_lists_options_and_explains_free_typing(fake_input):
    fake = fake_input("1")
    tools.ask_user("Which one?", ["Alpha", "Beta"])

    prompt = fake.prompts[0]
    assert "Which one?" in prompt
    assert "1) Alpha" in prompt
    assert "2) Beta" in prompt
    assert tools.ASK_USER_HINT in prompt
    assert "Type an option number or the answer you want to give the AI" in prompt


def test_schema_tells_the_model_that_typed_answers_come_back():
    schema = next(
        t for t in tools.TOOLS_DESCRIPTION
        if t["function"]["name"] == "ask_user"
    )
    description = schema["function"]["description"]
    assert "typed answer is returned to you exactly as written" in description


# ── Answers ──────────────────────────────────────────────────────────────────

def test_number_selects_the_matching_option(fake_input):
    fake_input("2")
    assert tools.ask_user("Which one?", ["Alpha", "Beta"]) == "Beta"


def test_typed_answer_is_returned_to_the_ai_verbatim(fake_input):
    fake_input("Neither — build me a third one")
    answer = tools.ask_user("Which one?", ["Alpha", "Beta"])
    assert answer == "Neither — build me a third one"


def test_typed_answer_wins_over_a_numeric_option(fake_input):
    """A typed answer that starts with digits is still an answer."""
    fake_input("42 is the answer")
    assert tools.ask_user("How many?", ["10", "20"]) == "42 is the answer"


def test_out_of_range_number_is_kept_as_the_users_own_answer(fake_input):
    fake_input("9")
    assert tools.ask_user("Which one?", ["Alpha", "Beta"]) == "9"


def test_number_with_trailing_punctuation_still_selects(fake_input):
    fake_input("2.")
    assert tools.ask_user("Which one?", ["Alpha", "Beta"]) == "Beta"


def test_free_text_without_options_is_returned_as_typed(fake_input):
    fake_input("  go with postgres  ")
    assert tools.ask_user("Which database?") == "go with postgres"


def test_empty_input_falls_back_to_the_default_option(fake_input):
    fake_input("")
    assert tools.ask_user("Which one?", ["Alpha", "Beta"], default=2) == "Beta"


def test_empty_input_falls_back_to_a_string_default(fake_input):
    fake_input("")
    assert tools.ask_user("Which one?", default="skip") == "skip"


def test_numbers_only_question_asks_again_on_a_bad_number(fake_input):
    fake = fake_input("9", "1")
    answer = tools.ask_user("Which one?", ["Alpha", "Beta"], allow_custom=False)
    assert answer == "Alpha"
    assert len(fake.prompts) == 2


def test_numbers_only_question_rejects_a_typed_answer(fake_input):
    fake = fake_input("my own answer", "1")
    answer = tools.ask_user("Which one?", ["Alpha", "Beta"], allow_custom=False)
    assert answer == "Alpha"
    assert len(fake.prompts) == 2


def test_cancelled_on_eof(fake_input):
    fake_input()  # no answers queued -> EOFError
    assert tools.ask_user("Which one?", ["Alpha"]) == "[USER CANCELLED]"


# ── The whole chain ──────────────────────────────────────────────────────────

def test_dispatch_hands_the_typed_answer_to_the_model(fake_input):
    """Model calls the tool -> user types an answer -> model gets that answer."""
    from core import llm_client

    fake_input("do whatever you think is best")
    tool_call = {
        "function": {
            "name": "ask_user",
            "arguments": json.dumps({
                "question": "How should I proceed?",
                "options": ["Option A", "Option B"],
            }),
        }
    }
    result = llm_client._dispatch_tool(tool_call)
    assert result == "do whatever you think is best"
