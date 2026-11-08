"""The decision chain has to stay honest.

Every invariant below is the kind that breaks *silently*: the wake check
answers "no" for every utterance, the Gemini fallback never runs because the
request is malformed, or the tool returns a fabricated confidence.

1. the Decisions API is called with the contract Respan actually accepts —
   a noul question whose `instructions` is a string and whose `criteria` is
   an object of plain strings keyed "true"/"false";
2. one request may carry several questions, and they cost one request;
3. the documented *choice* and *score* question types are sent only to a
   route that implements them — the free Respan model answers noul questions
   only, so a typed question must never reach it;
4. a scale question is derived from cumulative noul questions and the
   derived distribution has to add up;
5. the chat routes never send NVIDIA's `enable_thinking` switch to Gemini,
   whose endpoint rejects the unknown field with HTTP 400, and never ask for
   a budget below 64 tokens, which comes back with no content at all;
6. `is_wake_relevant` fails closed when every route fails;
7. `make_decision` returns the route that actually answered, and never
   invents a confidence it did not measure;
8. the tool is dispatchable and its registry entry is free-only.
"""
import io
import json
import os
import sys
import urllib.error

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "core") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "core"))

from core import models as models_mod  # noqa: E402
from core import tools as tools_mod  # noqa: E402


# ── 1. the Decisions API contract ────────────────────────────────────────────

def test_decisions_payload_matches_the_live_contract(monkeypatch):
    """Respan accepts noul questions only, with string criteria."""
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode())
        seen["auth"] = request.get_header("Authorization")

        class _Resp:
            def read(self):
                return json.dumps({
                    "answers": {"decision": {"type": "noul", "noul": 0.93}},
                    "model": "respan/span-01-lite-20260925",
                    "usage": {"cost": 0},
                }).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp()

    monkeypatch.setattr(tools_mod.urllib.request, "urlopen", fake_urlopen)
    import llm_client
    monkeypatch.setattr(llm_client, "API_KEYS", {"openrouter": ["k"]})

    slot = next(s for s in models_mod.DECISION_MODEL_SLOTS
                if s.get("api") == "decisions")
    probability, served = tools_mod._decision_call_decisions(
        slot, "the state", "the instructions",
        {"true": "yes means yes", "false": "no means no"}, 10.0)

    assert probability == pytest.approx(0.93)
    assert served == "respan/span-01-lite-20260925"
    assert seen["url"] == models_mod.DECISION_API_URL

    question = seen["body"]["questions"]["decision"]
    assert question["type"] == "noul"
    assert isinstance(question["instructions"], str)
    assert question["instructions"] == "the instructions"
    assert isinstance(question["criteria"], dict)
    assert set(question["criteria"]) == {"true", "false"}
    assert all(isinstance(v, str) for v in question["criteria"].values())
    assert seen["body"]["state"] == "the state"


# ── 2. the chat routes ───────────────────────────────────────────────────────

def _fake_openai(monkeypatch, captured, content="YES"):
    class _Msg:
        def __init__(self, c):
            self.content = c

    class _Choice:
        def __init__(self):
            self.message = _Msg(content)
            self.finish_reason = "stop"

    class _Resp:
        def __init__(self):
            self.choices = [_Choice()]

    class FakeClient:
        def __init__(self, api_key=None, base_url=None, timeout=None):
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            captured["timeout"] = timeout
            self.chat = self
            self.completions = self

        def create(self, **kwargs):
            captured.update(kwargs)
            return _Resp()

    import openai
    monkeypatch.setattr(openai, "OpenAI", FakeClient)


def test_gemini_route_sends_no_nvidia_thinking_switch(monkeypatch):
    """Gemini rejects chat_template_kwargs with HTTP 400."""
    captured = {}
    _fake_openai(monkeypatch, captured)
    import llm_client
    monkeypatch.setattr(llm_client, "API_KEYS", {"google": ["k"]})

    google_slot = next(s for s in models_mod.DECISION_MODEL_SLOTS
                       if s["provider_id"] == "google")
    assert "enable_thinking" not in google_slot

    content, served = tools_mod._decision_call_chat(
        google_slot, [{"role": "user", "content": "x"}], 128, 10.0)

    assert content == "YES"
    assert served == "gemini-2.5-flash"
    assert "extra_body" not in captured
    assert captured["max_tokens"] == 128


def test_nvidia_route_may_send_the_thinking_switch(monkeypatch):
    captured = {}
    _fake_openai(monkeypatch, captured)
    import llm_client
    monkeypatch.setattr(llm_client, "API_KEYS", {"nvidia": ["k"]})

    nvidia_slot = next(s for s in models_mod.DECISION_MODEL_SLOTS
                       if s["provider_id"] == "nvidia")
    tools_mod._decision_call_chat(
        nvidia_slot, [{"role": "user", "content": "x"}], 128, 10.0)

    assert captured["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}}


def test_gemini_budget_is_never_below_the_thinking_floor(monkeypatch):
    """Gemini spends part of the budget on thinking: <64 tokens returns nothing."""
    captured = {}
    _fake_openai(monkeypatch, captured)
    import llm_client
    monkeypatch.setattr(llm_client, "API_KEYS", {"google": ["k"]})

    google_slot = next(s for s in models_mod.DECISION_MODEL_SLOTS
                       if s["provider_id"] == "google")
    tools_mod._decision_call_chat(
        google_slot, [{"role": "user", "content": "x"}], 4, 10.0)

    assert captured["max_tokens"] >= 64


def test_empty_content_is_a_failure_not_a_no(monkeypatch):
    """A reasoning model that spends the whole budget has not answered."""
    captured = {}
    _fake_openai(monkeypatch, captured, content=None)
    import llm_client
    monkeypatch.setattr(llm_client, "API_KEYS", {"google": ["k"]})

    google_slot = next(s for s in models_mod.DECISION_MODEL_SLOTS
                       if s["provider_id"] == "google")
    content, served = tools_mod._decision_call_chat(
        google_slot, [{"role": "user", "content": "x"}], 128, 10.0)

    assert content is None
    assert served == "gemini-2.5-flash"


# ── 3. the wake check fails closed ───────────────────────────────────────────

def test_is_wake_relevant_fails_closed(monkeypatch):
    monkeypatch.setattr(tools_mod, "_wake_relevance", lambda text: (False, None, "none"))
    assert tools_mod.is_wake_relevant("orion") is False


def test_is_wake_relevant_uses_the_decision_model_first(monkeypatch):
    """The decision model answers the wake question; Gemini is the fallback."""
    order = []

    def fake_decisions(slot, state, instructions, criteria, timeout):
        order.append(("decisions", slot["name"]))
        return 0.97, "respan/span-01-lite-20260925"

    def fake_chat(slot, messages, max_tokens, timeout):
        order.append(("chat", slot["name"]))
        return "YES", slot["name"]

    monkeypatch.setattr(tools_mod, "_decision_call_decisions", fake_decisions)
    monkeypatch.setattr(tools_mod, "_decision_call_chat", fake_chat)

    assert tools_mod.is_wake_relevant("orion, what is the weather?") is True
    assert order[0][0] == "decisions"
    assert order[0][1] == "respan/span-01-lite:free"


def test_wake_falls_back_to_gemini_when_the_decision_model_fails(monkeypatch):
    def fake_decisions(slot, state, instructions, criteria, timeout):
        return None, "respan/span-01-lite:free"

    def fake_chat(slot, messages, max_tokens, timeout):
        return "NO", slot["name"]

    monkeypatch.setattr(tools_mod, "_decision_call_decisions", fake_decisions)
    monkeypatch.setattr(tools_mod, "_decision_call_chat", fake_chat)

    relevant, probability, model = tools_mod._wake_relevance("the weather is nice")
    assert relevant is False
    assert model == "gemini-2.5-flash"


# ── 4. make_decision ─────────────────────────────────────────────────────────

def test_make_decision_yes_no_reports_the_real_probability(monkeypatch):
    def fake_decisions(slot, state, instructions, criteria, timeout):
        return 0.82, "respan/span-01-lite-20260925"

    monkeypatch.setattr(tools_mod, "_decision_call_decisions", fake_decisions)

    result = tools_mod.make_decision("Is the build green?")
    assert result["answer"] == "YES"
    assert result["model"] == "respan/span-01-lite-20260925"
    assert result["confidence"] == pytest.approx(0.82)


def test_make_decision_choice_never_uses_the_decisions_api(monkeypatch):
    """Respan answers noul questions only — a choice must not go there."""
    def boom(*args, **kwargs):
        raise AssertionError("the decisions API cannot answer a choice")

    def fake_chat(slot, messages, max_tokens, timeout):
        return ("ANSWER: option B\nWHY: it is cheaper\nCONFIDENCE: 0.7",
                slot["name"])

    monkeypatch.setattr(tools_mod, "_decision_call_questions", boom)
    monkeypatch.setattr(tools_mod, "_decision_call_chat", fake_chat)

    result = tools_mod.make_decision(
        "Which option?", options=["option A", "option B"])
    assert result["answer"] == "option B"
    assert result["why"] == "it is cheaper"
    assert result["confidence"] == pytest.approx(0.7)


def test_make_decision_without_a_question_says_so():
    result = tools_mod.make_decision("")
    assert result["answer"] == ""
    assert result["model"] == "none"


def test_make_decision_reports_no_confidence_it_did_not_measure(monkeypatch):
    def fake_decisions(slot, state, instructions, criteria, timeout):
        return None, "respan/span-01-lite:free"

    def fake_chat(slot, messages, max_tokens, timeout):
        return "ANSWER: YES\nWHY: because", slot["name"]

    monkeypatch.setattr(tools_mod, "_decision_call_decisions", fake_decisions)
    monkeypatch.setattr(tools_mod, "_decision_call_chat", fake_chat)

    result = tools_mod.make_decision("Is it safe?")
    assert result["answer"] == "YES"
    assert result["model"] == "gemini-2.5-flash"
    # The chat route reported no confidence, so none is invented.
    assert result["confidence"] == 0.0


# ── 3b. the documented typed contract ────────────────────────────────────────

def _typed_slot(*types):
    """A decisions route that implements the given question types."""
    return {
        "provider_id": "openrouter",
        "name": "fake/typed-decision-model",
        "api": "decisions",
        "question": tuple(types),
        "timeout": 5.0,
    }


def test_one_request_can_carry_several_questions(monkeypatch):
    """Several questions in one call cost one request against the allowance."""
    calls = []
    seen = {}

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        seen["body"] = json.loads(request.data.decode())

        class _Resp:
            def read(self):
                return json.dumps({
                    "answers": {
                        "one": {"type": "noul", "noul": 0.2},
                        "two": {"type": "noul", "noul": 0.8},
                    },
                    "model": "respan/span-01-lite-20260925",
                }).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp()

    monkeypatch.setattr(tools_mod.urllib.request, "urlopen", fake_urlopen)
    import llm_client
    monkeypatch.setattr(llm_client, "API_KEYS", {"openrouter": ["k"]})

    slot = next(s for s in models_mod.DECISION_MODEL_SLOTS
                if s.get("api") == "decisions")
    answers, served = tools_mod._decision_call_questions(
        slot, "the state",
        {"one": {"type": "noul", "instructions": "a?",
                 "criteria": {"true": "y", "false": "n"}},
         "two": {"type": "noul", "instructions": "b?",
                 "criteria": {"true": "y", "false": "n"}}},
        10.0)

    assert len(calls) == 1
    assert set(seen["body"]["questions"]) == {"one", "two"}
    assert answers["one"]["noul"] == pytest.approx(0.2)
    assert answers["two"]["noul"] == pytest.approx(0.8)
    assert served == "respan/span-01-lite-20260925"


def test_the_free_route_is_never_sent_a_typed_question():
    """Respan answers noul questions only — the registry must say so."""
    decisions_slots = [s for s in models_mod.DECISION_MODEL_SLOTS
                       if s.get("api") == "decisions"]
    assert decisions_slots
    for slot in decisions_slots:
        assert "choice" not in slot.get("question", ())
        assert "score" not in slot.get("question", ())


def test_a_bad_request_is_not_retried_on_every_key(monkeypatch):
    """HTTP 400 is a property of the request, not of the key."""
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        raise urllib.error.HTTPError(
            request.full_url, 400, "Bad Request",
            {}, io.BytesIO(b'{"error":{"message":"Respan only accepts noul '
                           b'questions ..."}}'))

    monkeypatch.setattr(tools_mod.urllib.request, "urlopen", fake_urlopen)
    import llm_client
    monkeypatch.setattr(llm_client, "API_KEYS", {"openrouter": ["k1", "k2"]})

    slot = next(s for s in models_mod.DECISION_MODEL_SLOTS
                if s.get("api") == "decisions")
    answers, _served = tools_mod._decision_call_questions(
        slot, "the state",
        {"decision": {"type": "noul", "instructions": "a?",
                      "criteria": {"true": "y", "false": "n"}}},
        10.0)

    assert answers is None
    assert len(calls) == 1


def test_a_native_choice_question_carries_the_documented_payload(monkeypatch):
    captured = {}

    def fake_questions(slot, state, questions, timeout):
        captured["questions"] = questions
        captured["state"] = state
        return {
            "choice": {"type": "choice", "choice": "billing",
                       "probabilities": {"billing": 0.96, "technical": 0.04},
                       "confidence": 0.92},
        }, "typesafe/jev-1.13-20260917"

    monkeypatch.setattr(tools_mod, "_decision_call_questions", fake_questions)

    result = tools_mod._decision_call_native_choice(
        _typed_slot("choice"), "the state", "Which team?",
        ["billing", "technical"], "", 5.0)

    question = captured["questions"]["choice"]
    assert question["type"] == "choice"
    assert isinstance(question["criteria"], dict)
    assert set(question["criteria"]) == {"billing", "technical"}
    assert all(isinstance(v, str) for v in question["criteria"].values())
    assert result["answer"] == "billing"
    assert result["probabilities"]["billing"] == pytest.approx(0.96)
    assert result["confidence"] == pytest.approx(0.92)


def test_a_native_score_question_carries_the_documented_payload(monkeypatch):
    captured = {}

    def fake_questions(slot, state, questions, timeout):
        captured["questions"] = questions
        return {
            "score": {"type": "score", "score": 1.99,
                      "legend": {"0": "Calm", "1": "Frustrated",
                                 "2": "Very angry"},
                      "probabilities": {"0": 0, "1": 0.01, "2": 0.99},
                      "confidence": 0.99},
        }, "typesafe/jev-1.13-20260917"

    monkeypatch.setattr(tools_mod, "_decision_call_questions", fake_questions)

    result = tools_mod._decision_call_native_score(
        _typed_slot("score"), "the state", "How angry?",
        ["Calm", "Frustrated", "Very angry"], "", 5.0)

    question = captured["questions"]["score"]
    assert question["type"] == "score"
    assert question["criteria"] == ["Calm", "Frustrated", "Very angry"]
    assert result["answer"] == "Very angry"
    assert result["score"] == 2
    assert result["confidence"] == pytest.approx(0.99)


def test_make_decision_uses_a_native_choice_when_a_route_has_one(monkeypatch):
    monkeypatch.setattr(models_mod, "DECISION_MODEL_SLOTS",
                        [_typed_slot("choice")])

    def fake_questions(slot, state, questions, timeout):
        assert questions["choice"]["type"] == "choice"
        return {
            "choice": {"type": "choice", "choice": "option B",
                       "probabilities": {"option A": 0.25, "option B": 0.75},
                       "confidence": 0.75},
        }, "fake/typed-decision-model"

    monkeypatch.setattr(tools_mod, "_decision_call_questions", fake_questions)

    result = tools_mod.make_decision(
        "Which option?", options=["option A", "option B"])
    assert result["answer"] == "option B"
    assert result["model"] == "fake/typed-decision-model"
    assert result["confidence"] == pytest.approx(0.75)
    assert result["probabilities"]["option B"] == pytest.approx(0.75)


# ── 4. a scale is derived from cumulative noul questions ─────────────────────

def test_a_scale_is_derived_from_cumulative_noul_questions(monkeypatch):
    captured = {}

    def fake_questions(slot, state, questions, timeout):
        captured["questions"] = questions
        return {
            "atleast_1": {"type": "noul", "noul": 0.9},
            "atleast_2": {"type": "noul", "noul": 0.5},
        }, "respan/span-01-lite-20260925"

    monkeypatch.setattr(tools_mod, "_decision_call_questions", fake_questions)

    result = tools_mod.make_decision(
        "How angry is the customer?",
        scale=["Calm", "Frustrated", "Very angry"])

    # One request, one cumulative question per level above the bottom.
    assert set(captured["questions"]) == {"atleast_1", "atleast_2"}
    for question in captured["questions"].values():
        assert question["type"] == "noul"
        assert set(question["criteria"]) == {"true", "false"}

    # P(0) = 1 - P(>=1) = 0.1, P(1) = 0.9 - 0.5 = 0.4, P(2) = 0.5
    probabilities = result["probabilities"]
    assert probabilities["Calm"] == pytest.approx(0.1)
    assert probabilities["Frustrated"] == pytest.approx(0.4)
    assert probabilities["Very angry"] == pytest.approx(0.5)
    assert sum(probabilities.values()) == pytest.approx(1.0)
    assert result["answer"] == "Very angry"
    assert result["score"] == 2
    assert result["model"] == "respan/span-01-lite-20260925"


def test_a_derived_scale_reports_no_confidence_it_did_not_measure(monkeypatch):
    """A missing band means a failed derivation, not a zero probability."""
    def fake_questions(slot, state, questions, timeout):
        return {"atleast_1": {"type": "noul", "noul": 0.9}}, "respan"

    monkeypatch.setattr(tools_mod, "_decision_call_questions", fake_questions)

    def fake_chat(slot, messages, max_tokens, timeout):
        return "ANSWER: Frustrated\nWHY: it reads as annoyed", slot["name"]

    monkeypatch.setattr(tools_mod, "_decision_call_chat", fake_chat)

    result = tools_mod.make_decision(
        "How angry is the customer?",
        scale=["Calm", "Frustrated", "Very angry"])
    assert result["answer"] == "Frustrated"
    assert result["model"] != "respan"
    assert result["confidence"] == 0.0


def test_a_scale_falls_back_to_the_chat_routes(monkeypatch):
    def fake_questions(slot, state, questions, timeout):
        return None, "respan/span-01-lite-20260925"

    monkeypatch.setattr(tools_mod, "_decision_call_questions", fake_questions)

    def fake_chat(slot, messages, max_tokens, timeout):
        return ("ANSWER: Frustrated\nWHY: it reads as annoyed\n"
                "CONFIDENCE: 0.6", slot["name"])

    monkeypatch.setattr(tools_mod, "_decision_call_chat", fake_chat)

    result = tools_mod.make_decision(
        "How angry is the customer?",
        scale=["Calm", "Frustrated", "Very angry"])
    assert result["answer"] == "Frustrated"
    assert result["confidence"] == pytest.approx(0.6)


# ── 5. the registry and the dispatch route ───────────────────────────────────

def test_decision_routes_are_free_only():
    assert models_mod.free_route_violations() == []


def test_decision_registry_is_covered_by_the_free_check():
    """A paid decision route must be caught by the machine check."""
    offenders = models_mod.free_route_violations({
        "DECISION_MODEL_SLOTS": [
            {"provider_id": "openrouter", "name": "some/paid-model"},
        ],
    })
    assert offenders == [("openrouter", "some/paid-model")]


def test_the_paid_typed_decision_model_is_not_registered():
    """The one model that answers choice/score natively is paid."""
    registered = {slot["name"] for slot in models_mod.DECISION_MODEL_SLOTS}
    for model in models_mod.PAID_TYPED_DECISION_MODELS:
        assert model not in registered
        assert models_mod.is_free_route("openrouter", model) is False


def test_make_decision_is_dispatchable(monkeypatch):
    from core import llm_client

    def fake_make_decision(question, options=None, scale=None, context="",
                           criteria=""):
        return {"answer": "YES", "why": "because", "model": "test",
                "confidence": 0.9}

    monkeypatch.setattr(llm_client, "make_decision", fake_make_decision)
    monkeypatch.setattr(llm_client.plugin_manager, "is_tool_allowed",
                        lambda name: True)

    out = llm_client._dispatch_tool({
        "function": {
            "name": "make_decision",
            "arguments": json.dumps({"question": "Is it safe?"}),
        }
    })
    assert json.loads(out)["answer"] == "YES"


def test_the_dispatch_route_passes_the_scale_through(monkeypatch):
    from core import llm_client

    seen = {}

    def fake_make_decision(question, options=None, scale=None, context="",
                           criteria=""):
        seen["scale"] = scale
        return {"answer": "medium", "why": "because", "model": "test",
                "confidence": 0.4}

    monkeypatch.setattr(llm_client, "make_decision", fake_make_decision)
    monkeypatch.setattr(llm_client.plugin_manager, "is_tool_allowed",
                        lambda name: True)

    out = llm_client._dispatch_tool({
        "function": {
            "name": "make_decision",
            "arguments": json.dumps({
                "question": "How bad is it?",
                "scale": ["low", "medium", "high"],
            }),
        }
    })
    assert seen["scale"] == ["low", "medium", "high"]
    assert json.loads(out)["answer"] == "medium"



