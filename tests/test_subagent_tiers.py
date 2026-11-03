"""Tests for the intelligence-tier routing of delegate_subtask.

The sub-agent tool no longer takes a model id: it takes an intelligence level
("high" / "medium" / "low") and core/models.py maps that level onto a list of
models.  These tests lock in the properties that make that safe:

  * only tool-calling capable models are routable,
  * every model of the agent model list is reachable through some tier,
  * the tiers are ordered by capability,
  * the tool exposes "intelligence" and no longer exposes "model"/"max_tokens",
  * the output budget is never a hard truncation: it is lowered only when the
    provider says the account cannot afford it, and a truncated answer is
    continued on the next model.
"""
import json
import os
import sys
import types

import pytest

import core.models as models
import core.tools as tools


# ── registry invariants ───────────────────────────────────────────────────────

def test_every_tier_model_can_call_tools():
    """Only tool-calling capable models may be routed a delegated task."""
    offenders = [
        (tier, entry["provider_id"], entry["name"])
        for tier, entries in models.SUBAGENT_TIERS.items()
        for entry in entries
        if not models.model_supports_tools(entry["provider_id"], entry["name"])
    ]
    assert offenders == [], f"models without verified tool calling: {offenders}"


def test_the_tool_calling_gate_has_live_evidence():
    """The gate must stay backed by a recorded probe, not by a claim.

    tests/fixtures/subagent_tool_calling.json holds the result of the live
    probe: every model in it really was asked to call a tool and really
    answered with a tool call.  A model that is added to the gate without
    such a record fails this test.
    """
    fixture_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "fixtures", "subagent_tool_calling.json",
    )
    with open(fixture_path, encoding="utf-8") as fh:
        evidence = json.load(fh)["models"]

    missing = []
    for provider_id, names in models.TOOL_CALLING_MODELS.items():
        for name in sorted(names):
            record = evidence.get(f"{provider_id}/{name}")
            if not record or record.get("status") != "ok":
                missing.append(f"{provider_id}/{name}")
                continue
            if not (record.get("tools_accepted") or record.get("emitted_tool_call")):
                missing.append(f"{provider_id}/{name} (no tool call in the record)")
    assert missing == [], f"gate entries without live evidence: {missing}"


def test_every_agent_model_is_reachable_through_a_tier():
    """The agent model list must be fully covered by the tier registry."""
    routed = {
        (entry["provider_id"], entry["name"])
        for entries in models.SUBAGENT_TIERS.values()
        for entry in entries
    }
    missing = [
        (slot["provider_id"], slot["name"])
        for slot in models.AGENT_MODEL_SLOTS
        if (slot["provider_id"], slot["name"]) not in routed
    ]
    assert missing == [], f"agent models missing from the tiers: {missing}"


def test_tier_ordering_follows_capability():
    """High tier = the largest/reasoning models, low tier = the cheap end."""
    high = [(e["provider_id"], e["name"]) for e in models.SUBAGENT_TIERS["high"]]
    medium = [(e["provider_id"], e["name"]) for e in models.SUBAGENT_TIERS["medium"]]
    low = [(e["provider_id"], e["name"]) for e in models.SUBAGENT_TIERS["low"]]

    # The headline free models must be in the high tier.
    for expected in [
        ("nvidia", "minimaxai/minimax-m3.1-flash"),
        ("nvidia", "nvidia/nemotron-3-ultra-550b-a55b"),
        ("ollama", "nemotron-3-ultra:cloud"),
        ("groq", "qwen/qwen3.8-27b"),
        ("google", "gemini-3.8-flash"),
    ]:
        assert expected in high, f"{expected} must be in the high tier"

    # GPT-OSS 120B is a tier down, Gemini 2.5 Flash is the cheap end.
    assert ("groq", "openai/gpt-oss-120b") in medium
    assert ("ollama", "gpt-oss:120b-cloud") in medium
    assert ("google", "gemini-2.5-flash") in low

    # And the strongest models must come first inside their tier.
    # The tier membership follows the measured Artificial Analysis indices
    # (Gemini 3.8 Flash intelligence 30.5 / coding 76.3 > MiniMax M3.1 Flash
    # 28.9 > Qwen3.8-27B coding 68.1 > Nemotron 3.5 Super VL 13.6); the order
    # inside the tier puts the fastest of them first, because a route that
    # needs minutes for its first token would otherwise stall every delegation.
    assert high[0] == ("google", "gemini-3.8-flash")
    assert high[1] == ("nvidia", "minimaxai/minimax-m3.1-flash")
    assert high[2] == ("groq", "qwen/qwen3.8-27b")
    # The slowest route (measured > 140 s for the first token) comes last.
    assert high[-1] == ("nvidia", "z-ai/glm-5.3-flash")

    # The cheap end of the pool starts with the smallest Gemini.
    assert low[0] == ("google", "gemini-2.5-flash")
    assert medium[0] == ("ollama", "gpt-oss:120b-cloud")

    # A frontier model must never sit in the medium or low tier.
    frontier = {("google", "gemini-3.8-flash"),
                ("nvidia", "minimaxai/minimax-m3.1-flash"),
                ("nvidia", "nvidia/nemotron-3-ultra-550b-a55b")}
    assert not frontier & set(medium)
    assert not frontier & set(low)


def test_no_registered_route_is_paid():
    """Termux-AI is built for free AI — no registry may hold a paid route.

    This is the regression guard for the paid models that used to sit in the
    sub-agent list (OpenRouter's Gemini 4 Argon / Kimi K3 / GLM-5.3 and
    Ollama's minimax-m3 / glm-5.3 / kimi-k3).  Every route in the chat
    rotation, the agent rotation and the tiers must pass the free check.
    """
    offenders = models.free_route_violations()
    assert offenders == [], f"paid routes registered: {offenders}"


def test_the_paid_denylist_rejects_the_models_that_were_removed():
    """The known-paid routes must stay rejected."""
    for provider_id, name in [
        ("openrouter", "google/gemini-4-argon"),
        ("openrouter", "moonshotai/kimi-k3"),
        ("openrouter", "z-ai/glm-5.3"),
        ("openrouter", "minimax/minimax-m3.1-flash"),
        ("ollama", "minimax-m3"),
        ("ollama", "glm-5.3"),
        ("ollama", "kimi-k3"),
        ("google", "gemini-4-argon"),
        ("google", "gemini-3.1-pro-preview"),
    ]:
        assert not models.is_free_route(provider_id, name), \
            f"{provider_id}/{name} is paid and must not be routable"


def test_the_free_check_accepts_every_registered_free_route():
    """The free check must not be so strict that it rejects a free route."""
    for provider_id, name in [
        ("openrouter", "cohere/north-mini-code:free"),
        ("openrouter", "nvidia/nemotron-3.ultra-550b-a55b:free"),
        ("ollama", "gpt-oss:120b-cloud"),
        ("ollama", "nemotron-3-ultra:cloud"),
        ("google", "gemini-3.8-flash"),
        ("google", "gemma-4-31b-it"),
        ("nvidia", "z-ai/glm-5.3-flash"),
        ("groq", "openai/gpt-oss-120b"),
    ]:
        assert models.is_free_route(provider_id, name), \
            f"{provider_id}/{name} is free and must stay routable"


def test_normalize_intelligence_level():
    assert models.normalize_intelligence_level("high") == "high"
    assert models.normalize_intelligence_level("HIGH") == "high"
    assert models.normalize_intelligence_level("  Medium ") == "medium"
    assert models.normalize_intelligence_level("low") == "low"
    # Unknown values fall back to the default tier instead of failing.
    assert models.normalize_intelligence_level("ultra") == models.DEFAULT_SUBAGENT_INTELLIGENCE
    assert models.normalize_intelligence_level(None) == models.DEFAULT_SUBAGENT_INTELLIGENCE
    assert models.normalize_intelligence_level("") == models.DEFAULT_SUBAGENT_INTELLIGENCE


def test_rotation_starts_with_requested_tier_then_degrades():
    rotation = models.get_subagent_rotation("medium")
    tiers = [entry["tier"] for entry in rotation]

    assert tiers[0] == "medium"
    # Every model of the requested tier comes before any other tier.
    first_other = next(i for i, t in enumerate(tiers) if t != "medium")
    assert tiers[:first_other] == ["medium"] * first_other
    # The ladder only ever goes down: a "medium" request must never be
    # served by a frontier model the caller did not ask for.
    assert "low" in tiers
    assert "high" not in tiers

    high_rotation = [e["tier"] for e in models.get_subagent_rotation("high")]
    assert high_rotation[0] == "high"
    assert high_rotation.index("medium") < high_rotation.index("low")

    low_rotation = [e["tier"] for e in models.get_subagent_rotation("low")]
    assert set(low_rotation) == {"low"}


# ── tool surface ──────────────────────────────────────────────────────────────

def _delegate_schema():
    for entry in tools.TOOLS_DESCRIPTION:
        if entry.get("function", {}).get("name") == "delegate_subtask":
            return entry["function"]
    raise AssertionError("delegate_subtask is not advertised")


def test_schema_exposes_intelligence_not_model_or_max_tokens():
    schema = _delegate_schema()
    props = schema["parameters"]["properties"]

    assert "intelligence" in props
    assert props["intelligence"]["enum"] == ["low", "medium", "high"]
    assert props["intelligence"]["default"] == "medium"
    assert "model" not in props
    assert "max_tokens" not in props
    assert schema["parameters"]["required"] == ["task"]


def test_function_signature_dropped_model_and_max_tokens():
    import inspect
    params = inspect.signature(tools.delegate_subtask).parameters
    assert "intelligence" in params
    assert "model" not in params
    assert "max_tokens" not in params


# ── behavioural tests with a faked provider ───────────────────────────────────

class _FakeMessage:
    def __init__(self, content="DONE", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning = None
        self.reasoning_content = None
        self.reasoning_details = None


class _FakeChoice:
    def __init__(self, content="DONE", finish_reason="stop", tool_calls=None):
        self.message = _FakeMessage(content, tool_calls)
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, content="DONE", finish_reason="stop"):
        self.choices = [_FakeChoice(content, finish_reason)]


class _FakeRaw:
    def __init__(self, response):
        self._response = response
        self.text = json.dumps({"choices": [{"message": {"content": response.choices[0].message.content}}]})

    def parse(self):
        return self._response


class _FakeCompletions:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.behaviour(kwargs)


class _FakeClient:
    def __init__(self, behaviour, **kwargs):
        self.init_kwargs = kwargs
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(
                with_raw_response=_FakeCompletions(behaviour)
            )
        )


@pytest.fixture
def fake_llm(monkeypatch):
    """Patch the OpenAI client used by delegate_subtask and the API keys."""
    import llm_client  # the top-level module core/tools.py imports at call time

    state = {"behaviour": None, "clients": []}

    def _factory(**kwargs):
        client = _FakeClient(state["behaviour"], **kwargs)
        state["clients"].append(client)
        return client

    monkeypatch.setattr(tools, "OpenAI", _factory)
    monkeypatch.setattr(llm_client, "API_KEYS", {"openrouter": ["k1"], "google": ["g1"]})
    monkeypatch.setattr(llm_client, "_sanitize_messages_for_provider",
                        lambda msgs, pid, model_name=None, **kw: list(msgs))
    return state


def _route_calls(state):
    calls = []
    for client in state["clients"]:
        calls.extend(client.chat.completions.with_raw_response.calls)
    return calls


def test_routes_to_the_high_tier_first(fake_llm):
    """A "high" delegation must start with the first high-tier model."""
    fake_llm["behaviour"] = lambda kwargs: _FakeRaw(_FakeResponse("answer"))

    out = tools.delegate_subtask(task="do the thing", intelligence="high")

    assert "answer" in out
    calls = _route_calls(fake_llm)
    assert calls, "the provider must have been called"
    # The fixture only has keys for some providers, so the first *usable*
    # high-tier route is the one that must be called.
    keyed = {"openrouter", "google"}
    expected = next(
        entry["name"]
        for entry in models.SUBAGENT_TIERS["high"]
        if entry["provider_id"] in keyed
    )
    assert calls[0]["model"] == expected
    assert "intelligence=high" in out


def test_low_level_starts_in_the_low_tier(fake_llm):
    fake_llm["behaviour"] = lambda kwargs: _FakeRaw(_FakeResponse("answer"))

    out = tools.delegate_subtask(task="quick lookup", intelligence="low")

    assert "answer" in out
    calls = _route_calls(fake_llm)
    assert calls[0]["model"] == models.SUBAGENT_TIERS["low"][0]["name"]


def test_falls_over_to_the_next_model_of_the_same_tier(fake_llm):
    """When the first model fails, the next model of the tier is used."""
    state = {"n": 0}

    def behaviour(kwargs):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("Error code: 503 - upstream unavailable")
        return _FakeRaw(_FakeResponse("second model answer"))

    fake_llm["behaviour"] = behaviour

    out = tools.delegate_subtask(task="do the thing", intelligence="medium")

    assert "second model answer" in out
    calls = _route_calls(fake_llm)
    assert len(calls) >= 2
    # The routes that can actually be called (the fake only has openrouter and
    # google keys) — the failover must stay inside the requested tier.
    rotation = [
        e for e in models.get_subagent_rotation("medium")
        if e["provider_id"] in {"openrouter", "google"}
    ]
    assert calls[0]["model"] == rotation[0]["name"]
    assert calls[1]["model"] == rotation[1]["name"]
    assert calls[0]["model"] != calls[1]["model"]


def test_output_budget_is_halved_when_the_account_cannot_afford_it(fake_llm, monkeypatch):
    """OpenRouter's "requires more credits, or fewer max_tokens" answer must
    lower the ceiling and retry the same route instead of failing over."""
    # Only OpenRouter keys: the halving applies to a route that carries a
    # registered output ceiling (the budgetless providers never send one).
    import llm_client
    monkeypatch.setattr(llm_client, "API_KEYS", {"openrouter": ["k1"]})

    seen_budgets = []

    def behaviour(kwargs):
        seen_budgets.append(kwargs.get("max_tokens"))
        if len(seen_budgets) == 1:
            raise RuntimeError(
                "Error code: 402 - {'error': {'message': \"This request requires "
                "more credits, or fewer max_tokens. You requested up to 32768 "
                "tokens, but your balance is only $0.02\"}}"
            )
        return _FakeRaw(_FakeResponse("answered after retry"))

    fake_llm["behaviour"] = behaviour

    out = tools.delegate_subtask(task="do the thing", intelligence="high")

    assert "answered after retry" in out
    assert len(seen_budgets) >= 2
    assert seen_budgets[1] == seen_budgets[0] // 2
    # The retry stayed on the same model.
    calls = _route_calls(fake_llm)
    assert calls[0]["model"] == calls[1]["model"]


def test_routes_without_a_budget_do_not_send_max_tokens(fake_llm):
    """A route registered with max_tokens=None must not send the field at all,
    so the model is never cut off by an artificial cap."""
    fake_llm["behaviour"] = lambda kwargs: _FakeRaw(_FakeResponse("answer"))

    # gemini-2.5-flash (google) is registered without a ceiling.
    tools.delegate_subtask(task="quick lookup", intelligence="low")

    calls = _route_calls(fake_llm)
    assert calls, "the provider must have been called"
    assert calls[0]["model"] == "gemini-2.5-flash"
    assert "max_tokens" not in calls[0]


def test_truncated_answer_is_continued_on_the_next_model(fake_llm):
    """A truncated answer must not be thrown away: the next model continues."""
    state = {"n": 0}

    def behaviour(kwargs):
        state["n"] += 1
        if state["n"] == 1:
            return _FakeRaw(_FakeResponse("partial answer that got cut", finish_reason="length"))
        return _FakeRaw(_FakeResponse("completed answer"))

    fake_llm["behaviour"] = behaviour

    out = tools.delegate_subtask(task="write a long report", intelligence="medium")

    assert "completed answer" in out
    calls = _route_calls(fake_llm)
    assert len(calls) >= 2
    # The continuation is requested with the partial text as an anchor.
    continuation = [
        m for m in calls[1]["messages"]
        if m.get("role") == "user" and "Continue" in str(m.get("content", ""))
    ]
    assert continuation, "the next model must be asked to continue the work"


def test_no_api_keys_is_reported_cleanly(fake_llm, monkeypatch):
    import llm_client
    monkeypatch.setattr(llm_client, "API_KEYS", {})
    out = tools.delegate_subtask(task="anything", intelligence="high")
    assert out.startswith("[ERROR]")
    assert "No API keys" in out


def test_no_output_cap_is_imposed_where_the_provider_allows_it(fake_llm, monkeypatch):
    """A sub-agent must not be cut off in the middle of a report.

    The caller does not know how much room the work needs, so no ceiling is
    sent for the providers that accept a request without one — the model
    writes until it is done.  OpenRouter is the exception: it checks the
    requested output budget against the account balance before it runs the
    request, so it needs an explicit number there.
    """
    seen = []

    def behaviour(kwargs):
        seen.append(kwargs.get("max_tokens"))
        return _FakeRaw(_FakeResponse("answer"))

    fake_llm["behaviour"] = behaviour

    import llm_client
    monkeypatch.setattr(llm_client, "API_KEYS", {
        "google": ["k1"], "groq": ["k1"], "nvidia": ["k1"], "ollama": ["k1"],
    })

    # A route without a registered ceiling must not receive a max_tokens.
    tools.delegate_subtask(task="write a long report", intelligence="low")
    assert seen, "the sub-agent never called the provider"
    assert seen[0] is None, f"an output cap was imposed: {seen[0]}"

    # OpenRouter and Groq keep an explicit ceiling — OpenRouter's balance
    # check needs a number and Groq truncates at 3072 tokens without one.
    for provider in ("openrouter", "groq"):
        assert all(
            entry["max_tokens"] is not None
            for entries in models.SUBAGENT_TIERS.values()
            for entry in entries
            if entry["provider_id"] == provider
        ), f"{provider} must carry an output ceiling"
    # Every other provider must not carry one.
    for provider in ("google", "nvidia", "ollama"):
        assert all(
            entry["max_tokens"] is None
            for entries in models.SUBAGENT_TIERS.values()
            for entry in entries
            if entry["provider_id"] == provider
        ), f"{provider} must not have an imposed output cap"


def test_a_huge_tool_result_is_capped_before_it_goes_back(fake_llm, monkeypatch):
    """A delegated worker has no context compressor.

    A tool result that is fed back unchanged can push the follow-up request
    over the provider's request limit, so it is shortened first — keeping the
    head and the tail, because the tail usually holds the summary.
    """
    big = "HEAD-MARKER\n" + ("x" * 50000) + "\nTAIL-MARKER"
    capped = tools._cap_sub_tool_result(big)
    assert len(capped) < len(big)
    assert capped.startswith("HEAD-MARKER")
    assert capped.rstrip().endswith("TAIL-MARKER")
    assert "truncated" in capped

    # A short result is passed through untouched.
    assert tools._cap_sub_tool_result("short") == "short"


def test_the_default_toolkit_is_small_enough_for_providers(fake_llm, monkeypatch):
    """The sub-AI must not be handed every schema in the system.

    The full tool set is ~46 KB of JSON, which pushes providers over their
    request-size limit (Groq answers 413) before the task is even read.
    """
    import llm_client
    monkeypatch.setattr(llm_client, "API_KEYS", {"google": ["g1"]})

    seen = {}

    def behaviour(kwargs):
        seen["tools"] = kwargs.get("tools") or []
        return _FakeRaw(_FakeResponse("done"))

    fake_llm["behaviour"] = behaviour

    out = tools.delegate_subtask(task="look around", intelligence="medium")

    assert "done" in out
    names = {t["function"]["name"] for t in seen["tools"]}
    assert names, "the sub-AI received no tools at all"
    assert "shell" in names and "read_file" in names
    # The plugin/messaging tools are not part of the default toolkit.
    assert "send_gmail_message" not in names
    assert len(seen["tools"]) <= 15


def test_an_explicit_tool_list_is_honoured(fake_llm, monkeypatch):
    import llm_client
    monkeypatch.setattr(llm_client, "API_KEYS", {"google": ["g1"]})

    seen = {}

    def behaviour(kwargs):
        seen["tools"] = kwargs.get("tools") or []
        return _FakeRaw(_FakeResponse("done"))

    fake_llm["behaviour"] = behaviour

    tools.delegate_subtask(task="t", intelligence="medium",
                           available_tools=["get_datetime", "read_file"])

    names = {t["function"]["name"] for t in seen["tools"]}
    assert names == {"get_datetime", "read_file"}


def test_a_learned_budget_ceiling_is_reused_for_the_next_key(fake_llm, monkeypatch):
    """A refused budget is not rediscovered from scratch for every key."""
    import llm_client
    monkeypatch.setattr(llm_client, "API_KEYS", {"openrouter": ["k1", "k2"]})

    budgets = []

    def behaviour(kwargs):
        budgets.append(kwargs.get("max_tokens"))
        # The first key can never afford anything; the second one can.
        if len(budgets) <= 5:
            raise RuntimeError(
                "Error code: 402 - {'error': {'message': 'This request requires "
                "more credits, or fewer max_tokens. You requested up to 32768 "
                "tokens, but your balance is only $0.02'}}"
            )
        return _FakeRaw(_FakeResponse("answered"))

    fake_llm["behaviour"] = behaviour

    out = tools.delegate_subtask(task="t", intelligence="high")

    assert "answered" in out
    # Key 1 walks the ceiling down 32768 -> 2048, then key 2 starts at the
    # learned ceiling instead of rediscovering the refusal from the top.
    assert budgets[:5] == [32768, 16384, 8192, 4096, 2048], budgets
    assert budgets[5] == 2048, budgets


def test_a_restricted_key_is_skipped_but_the_other_keys_still_run(fake_llm, monkeypatch):
    """A restricted organization blocks *that key*, not the whole provider.

    Verified live on 2026-10-30: of the five configured Groq keys, one
    answered normally and four answered "Organization has been restricted".
    The refusal therefore belongs to the key's own organization, so retiring
    the whole provider on the first refusal would throw away the key that
    works.
    """
    import llm_client
    monkeypatch.setattr(llm_client, "API_KEYS", {"groq": ["g1", "g2", "g3"]})

    state = {"n": 0}

    def behaviour(kwargs):
        state["n"] += 1
        raise RuntimeError("Error code: 400 - Organization has been restricted.")

    fake_llm["behaviour"] = behaviour

    out = tools.delegate_subtask(task="anything", intelligence="medium")

    assert out.startswith("[ERROR]")
    # Each key of the provider gets its turn — one attempt per key, and the
    # provider is not abandoned because of a single restricted key.
    assert state["n"] == 3, f"expected one attempt per key, got {state['n']}"


def test_a_working_key_is_still_used_after_a_restricted_one(fake_llm, monkeypatch):
    """One restricted Groq key must not disable the provider."""
    import llm_client
    monkeypatch.setattr(llm_client, "API_KEYS", {"groq": ["restricted", "working"]})

    seen = []

    def behaviour(kwargs):
        # The fake client is built per key, so the key that is used is the one
        # the caller passed in.
        seen.append(kwargs.get("model"))
        if len(seen) == 1:
            raise RuntimeError("Error code: 400 - Organization has been restricted.")
        return _FakeRaw(_FakeResponse("answered by the working key"))

    fake_llm["behaviour"] = behaviour

    out = tools.delegate_subtask(task="anything", intelligence="medium")

    assert "answered by the working key" in out
    assert len(seen) == 2


def test_an_empty_balance_route_fails_over_without_shrinking(fake_llm, monkeypatch):
    """A zero balance is never fixed by a smaller budget — move on instead."""
    import llm_client
    # Only OpenRouter keys: the budgeted route has to be the first *keyed*
    # route of the rotation, or the delegate never reaches it.
    monkeypatch.setattr(llm_client, "API_KEYS", {"openrouter": ["k1"]})

    # The route that carries an output ceiling — a smaller budget is what the
    # halving would try, so it is the route that must NOT be halved.
    budgeted = next(
        entry for entry in models.get_subagent_rotation("high")
        if entry["provider_id"] == "openrouter" and entry["max_tokens"]
    )

    calls = []

    def behaviour(kwargs):
        calls.append(kwargs.get("model"))
        if kwargs.get("model") == budgeted["name"]:
            raise RuntimeError(
                "Error code: 402 - {'error': {'message': 'This request requires "
                "more credits, or fewer max_tokens. You requested up to 32768 "
                "tokens, but your balance is only $0.00'}}"
            )
        return _FakeRaw(_FakeResponse("answered by the next model"))

    fake_llm["behaviour"] = behaviour

    out = tools.delegate_subtask(task="anything", intelligence="high")

    assert "answered by the next model" in out
    # The empty-balance route was asked exactly once — no halving storm.
    assert calls.count(budgeted["name"]) == 1, calls
    # And the refusal did not shrink the ceiling: the route was left alone.
    budget_calls = [c for c in _route_calls(fake_llm)
                    if c["model"] == budgeted["name"]]
    assert len(budget_calls) == 1
    assert budget_calls[0]["max_tokens"] == budgeted["max_tokens"]
