"""
Tests for the context compressor.

The feature under test: when the agent's context no longer fits the model's
context window, the *oldest* reasoning details, tool calls and tool outputs
are compressed into a digest, the raw text is archived under a stable id, and
the model can pull the raw text back and continue the work.

Covered here:

  1. The size estimate and the compression itself.
  2. The safety rules — the system prompt, the recent turns and the
     tool-call / tool-result pairing must survive.
  3. The archive — content-addressed, paged, retrievable by id.
  4. The llm_client integration — the context-error matcher, the context
     budget, and the fact that the shared model registry is never mutated.

No network access and no real API keys are used here.
"""
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(REPO_ROOT, "core")
for _p in (REPO_ROOT, CORE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import context_compressor as cc  # noqa: E402
from core import llm_client  # noqa: E402
from core.models import AGENT_MODEL_SLOTS, MODEL_SLOTS  # noqa: E402


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def archive(tmp_path, monkeypatch):
    """Redirect the archive away from the real logs directory."""
    monkeypatch.setattr(cc, "ARCHIVE_FILE", str(tmp_path / "archive.jsonl"))
    cc._load()
    yield
    cc._load()


# ── Helpers ────────────────────────────────────────────────────────────────

def system_msg(content="system prompt"):
    return {"role": "system", "content": content}


def user_msg(content="user text"):
    return {"role": "user", "content": content}


def assistant_msg(content="assistant reply"):
    return {"role": "assistant", "content": content}


def tool_call_msg(tid="c1", name="dummy", args="{}"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": tid,
            "type": "function",
            "function": {"name": name, "arguments": args},
        }],
    }


def tool_result_msg(tid="c1", content="tool result"):
    return {"role": "tool", "tool_call_id": tid, "content": content}


def pairing_ok(messages):
    """True when every tool message has its originating assistant tool call."""
    pending = set()
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tid = tc.get("id")
                if tid:
                    pending.add(tid)
        elif m.get("role") == "tool":
            tid = m.get("tool_call_id")
            if tid not in pending:
                return False
            pending.discard(tid)
    return True


def long_history(passes=4, size=4000):
    """A history of user / tool-call / tool-result / reply exchanges."""
    big = "X" * size
    msgs = [system_msg()]
    for i in range(passes):
        msgs.append(user_msg(f"q{i}: {big}"))
        msgs.append(tool_call_msg(tid=f"c{i}", args=json.dumps({"data": big})))
        msgs.append(tool_result_msg(tid=f"c{i}", content=big))
        msgs.append(assistant_msg(f"a{i}: {big}"))
    return msgs


# ── The estimate ───────────────────────────────────────────────────────────

def test_estimate_tokens_is_zero_for_an_empty_list():
    assert cc.estimate_tokens([]) == 0


def test_estimate_tokens_grows_with_content():
    small = cc.estimate_tokens([user_msg("hello")])
    large = cc.estimate_tokens([user_msg("hello" * 500)])
    assert small > 0
    assert large > small


# ── Compression ────────────────────────────────────────────────────────────

def test_compression_fits_the_budget(archive):
    msgs = long_history()
    before = cc.estimate_tokens(msgs)
    new_msgs, _, report = cc.fit_to_budget(msgs, budget_tokens=before // 4)
    assert report["compressed"] is True
    assert cc.estimate_tokens(new_msgs) < before
    assert report["estimated_tokens"] == cc.estimate_tokens(new_msgs)


def test_compression_leaves_a_short_history_alone_when_it_fits(archive):
    msgs = [system_msg(), user_msg("hi"), assistant_msg("hello")]
    new_msgs, _, report = cc.fit_to_budget(msgs, budget_tokens=10_000)
    assert report["compressed"] is False
    assert new_msgs == msgs


def test_the_system_prompt_is_never_compressed(archive):
    msgs = long_history()
    msgs[0] = system_msg("unique system prompt")
    new_msgs, _, report = cc.fit_to_budget(msgs, budget_tokens=100)
    assert report["compressed"] is True
    assert new_msgs[0] == {"role": "system", "content": "unique system prompt"}


def test_the_recent_messages_stay_raw(archive):
    msgs = long_history(passes=3, size=200)
    recent_u = user_msg("recent question")
    recent_a = assistant_msg("recent reply")
    msgs.extend([recent_u, recent_a])
    # The budget has to be able to hold the protected recent turns, or the
    # compressor is right to compress them too.
    new_msgs, _, report = cc.fit_to_budget(msgs, budget_tokens=500)
    assert report["compressed"] is True
    assert recent_u in new_msgs
    assert recent_a in new_msgs


def test_the_prompt_being_answered_survives_forced_compression(archive):
    msgs = long_history(passes=2, size=3000)
    prompt = user_msg("THE PROMPT")
    msgs.append(prompt)
    new_msgs, _, report = cc.compress_forced(msgs, head_len=len(msgs))
    assert report["compressed"] is True
    assert prompt in new_msgs
    assert report["estimated_tokens"] < cc.estimate_tokens(msgs) / 2


def test_tool_calls_keep_their_pairing(archive):
    msgs = long_history(passes=3)
    new_msgs, _, report = cc.fit_to_budget(msgs, budget_tokens=200)
    assert report["compressed"] is True
    assert pairing_ok(new_msgs) is True


def test_compression_never_raises_the_tool_result_count(archive):
    """A tool result may be stubbed away, but never orphaned."""
    msgs = long_history(passes=3)
    new_msgs, _, _ = cc.fit_to_budget(msgs, budget_tokens=200)
    tool_ids = [m.get("tool_call_id") for m in msgs if m.get("role") == "tool"]
    new_ids = [m.get("tool_call_id") for m in new_msgs if m.get("role") == "tool"]
    assert len(new_ids) <= len(tool_ids)
    assert set(new_ids).issubset(set(tool_ids))


def test_the_live_turn_has_priority_over_the_history(archive):
    """The turn in progress stays raw; the older history absorbs the squeeze."""
    msgs = long_history(passes=6, size=4000)
    prompt = user_msg("CURRENT QUESTION")
    msgs.append(prompt)
    head_len = len(msgs)
    live_call = tool_call_msg(tid="live", name="read_file")
    live_result = tool_result_msg(tid="live", content="file body " * 500)
    msgs.extend([live_call, live_result])

    new_msgs, new_head_len, report = cc.fit_to_budget(
        msgs, budget_tokens=cc.estimate_tokens(msgs) // 2, head_len=head_len)
    assert report["compressed"] is True
    assert prompt in new_msgs
    assert live_call in new_msgs
    assert live_result in new_msgs
    # The live turn still starts where the caller expects it to.
    assert new_msgs[new_head_len] == live_call


def test_the_raw_content_is_archived_and_retrievable(archive):
    big = "UNIQUE-RAW-MARKER " + "Y" * 3000
    msgs = [system_msg(), user_msg("ask"),
            tool_call_msg(tid="c0"), tool_result_msg(tid="c0", content=big)]
    new_msgs, _, report = cc.fit_to_budget(msgs, budget_tokens=50)
    assert report["archive_ids"]
    raw = json.loads(cc.retrieve_archive(report["archive_ids"][0]))
    assert "UNIQUE-RAW-MARKER" in raw["content"]


def test_the_same_region_is_archived_only_once(archive):
    msgs = long_history(passes=2, size=3000)
    _, _, first = cc.fit_to_budget(msgs, budget_tokens=100)
    _, _, second = cc.fit_to_budget(msgs, budget_tokens=100)
    assert first["archive_ids"] == second["archive_ids"]
    listing = json.loads(cc.list_archives())
    assert len([e for e in listing if e["id"] == first["archive_ids"][0]]) == 1


# ── The archive ────────────────────────────────────────────────────────────

def test_retrieve_archive_pages(archive):
    msgs = long_history(passes=1, size=4000)
    _, _, report = cc.fit_to_budget(msgs, budget_tokens=50)
    aid = report["archive_ids"][0]
    first = json.loads(cc.retrieve_archive(aid, limit=500))
    assert first["truncated"] is True
    assert isinstance(first["next_offset"], int)
    second = json.loads(cc.retrieve_archive(aid, offset=first["next_offset"],
                                           limit=500))
    assert second["content"] != first["content"]
    assert second["offset"] == first["next_offset"]


def test_retrieve_archive_reports_an_unknown_id(archive):
    result = json.loads(cc.retrieve_archive("A999"))
    assert "error" in result


def test_list_archives_includes_the_new_segment(archive):
    msgs = long_history(passes=2, size=3000)
    _, _, report = cc.fit_to_budget(msgs, budget_tokens=100)
    listing = json.loads(cc.list_archives())
    ids = [e["id"] for e in listing]
    assert report["archive_ids"][0] in ids


def test_the_archive_survives_a_reload(archive):
    msgs = long_history(passes=2, size=3000)
    _, _, report = cc.fit_to_budget(msgs, budget_tokens=100)
    aid = report["archive_ids"][0]
    cc._load()
    raw = json.loads(cc.retrieve_archive(aid))
    assert raw["id"] == aid
    assert raw["total_chars"] > 0


# ── The window the provider states ──────────────────────────────────────────

def test_the_provider_stated_window_is_parsed():
    """
    The provider is the only authority on its window.  The registry that used
    to live in core/models.py claimed 256K for a model whose endpoint enforces
    8K — a guess that cost the whole compression feature.
    """
    assert llm_client._parse_context_limit(
        Exception("This model's maximum context length is 8192 tokens")) == 8192
    assert llm_client._parse_context_limit(
        Exception("This endpoint's maximum context length is 262144 tokens. "
                  "However, you requested about 268119 tokens "
                  "(259030 of text input, 9089 of tool input).")) == 262_144
    assert llm_client._parse_context_limit(
        Exception("prompt is too long: 250000 tokens > 200000 maximum")) == 200_000


def test_an_unstated_window_is_not_guessed():
    """No number in the message means no number — the context is compressed
    as a whole instead of against an invented budget."""
    assert llm_client._parse_context_limit(
        Exception("Input length and max_tokens exceed context window")) is None
    assert llm_client._parse_context_limit(
        Exception("429 rate limit exceeded")) is None


def test_the_output_cap_is_clamped_into_the_window():
    """
    A slot that asks for more output tokens than the whole window leaves no
    room for the prompt at all — NVIDIA answered "maximum context length is
    8192 tokens" to a request that asked for 16384 output tokens.
    """
    assert llm_client._clamp_max_tokens(16384, 8192) == 2048
    assert llm_client._clamp_max_tokens(4096, 262_144) == 4096
    assert llm_client._clamp_max_tokens(None, 8192) is None
    # The tool catalogue shares the window with the prompt and the output.
    capped = llm_client._clamp_max_tokens(4096, 8192, tool_tokens=6000)
    assert 6000 + capped + 512 <= 8192
    # A catalogue that eats the whole window leaves only the floor.
    assert llm_client._clamp_max_tokens(4096, 8192, tool_tokens=8000) == 512


def test_the_input_budget_leaves_room_for_the_tools_and_the_output():
    """The window is shared: the catalogue and the output cap come off the
    top before the history is measured."""
    small = llm_client._context_input_budget(8192, 2048, 2000)
    assert 0 < small < 8192
    assert small <= 8192 - 2048 - 2000
    # A roomier window gives a roomier budget.
    assert (llm_client._context_input_budget(262_144, 2048, 2000)
            > small)


# ── The llm_client integration ─────────────────────────────────────────────

def test_context_error_matcher_ignores_unrelated_errors():
    assert llm_client._is_context_error(Exception("429 rate limit exceeded")) is False
    assert llm_client._is_context_error(Exception("401 unauthorized")) is False
    assert llm_client._is_context_error(Exception("invalid json body")) is False


def test_context_error_matcher_catches_real_context_errors():
    samples = [
        "This model's maximum context length is 8192 tokens",
        "context_length_exceeded",
        "Input length and max_tokens exceed context window",
        "prompt is too long: 250000 tokens > 200000 maximum",
        "the request exceeds the available context size",
    ]
    for text in samples:
        assert llm_client._is_context_error(Exception(text)) is True, text


def test_an_oversized_request_status_is_treated_as_a_context_error():
    """
    Some gateways answer an oversized body with 405 or 413 instead of a
    readable message.  The status alone is enough to start compressing.
    """
    for status in (405, 413, 422, 431):
        exc = Exception("Request rejected")
        exc.status_code = status
        assert llm_client._is_context_error(exc) is True, status


def test_an_oversized_request_status_does_not_hide_a_real_cause():
    """A quota or auth failure keeps its own handling, whatever the status."""
    exc = Exception("429 rate limit exceeded")
    exc.status_code = 429
    assert llm_client._is_context_error(exc) is False


def test_the_shared_model_registry_is_never_mutated():
    """The output cap must be reduced per request, never in the registry."""
    before = {id(s): s.get("max_tokens") for s in MODEL_SLOTS + AGENT_MODEL_SLOTS}
    source = open(os.path.join(CORE, "llm_client.py"), encoding="utf-8").read()
    assert 'slot_cfg["max_tokens"] =' not in source
    after = {id(s): s.get("max_tokens") for s in MODEL_SLOTS + AGENT_MODEL_SLOTS}
    assert before == after


def test_the_archive_tools_are_registered():
    names = [t["function"]["name"] for t in llm_client.TOOLS_DESCRIPTION]
    assert "retrieve_archive" in names
    assert "list_archives" in names


def test_the_archive_tools_are_dispatchable(archive):
    """The model reaches the archive through the normal tool dispatch."""
    msgs = long_history(passes=2, size=3000)
    _, _, report = cc.fit_to_budget(msgs, budget_tokens=100)
    aid = report["archive_ids"][0]

    out = llm_client._dispatch_tool({
        "function": {
            "name": "retrieve_archive",
            "arguments": json.dumps({"archive_id": aid, "limit": 2000}),
        }
    })
    assert isinstance(out, str)
    payload = json.loads(out)
    assert payload["id"] == aid
    assert payload["total_chars"] > 0

    listing = json.loads(llm_client._dispatch_tool({
        "function": {"name": "list_archives", "arguments": "{}"}
    }))
    assert aid in [e["id"] for e in listing]


# ── Regressions ────────────────────────────────────────────────────────────

def test_the_module_is_a_single_instance_under_both_import_names():
    """
    The codebase imports core modules under two names — "core.context_compressor"
    and the bare "context_compressor" — because llm_client.py puts core/ on
    sys.path and imports its siblings by their bare names.  Python loads a
    separate module object for each spelling, and this module keeps its
    archive index in memory: two copies meant two indexes, so a segment
    archived through one was invisible to the other and the model was told
    "Archive 'A1' not found" for a segment that demonstrably existed.
    """
    import context_compressor as bare
    assert bare is cc
    assert llm_client._cc is cc
    assert bare._index is cc._index


def test_the_chunk_store_is_also_a_single_instance():
    """The same split-brain guard for the chunk store behind list_chunks."""
    import context_manager as bare_cm
    from core import context_manager as pkg_cm
    assert bare_cm is pkg_cm
    assert llm_client._cm is pkg_cm


def test_a_lookup_that_misses_the_cache_checks_the_file(archive):
    """
    The index is a cache; the archive file is the truth.  An entry written by
    another process (or another instance) must still be found rather than
    reported as unknown.
    """
    entry = {
        "id": "A99", "ts": "2026-01-01T00:00:00", "label": "external",
        "level": 1, "chars": 11, "hash": "deadbeef",
        "messages": [{"role": "user", "content": "external segment"}],
    }
    with open(cc.ARCHIVE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    payload = json.loads(cc.retrieve_archive("A99"))
    assert payload["id"] == "A99"
    assert "external segment" in payload["content"]


def test_compressed_tool_call_arguments_stay_valid_json():
    """
    The provider parses the arguments of an assistant tool call.  A blind
    character cut leaves malformed JSON behind and the request is rejected —
    the compression would cause the very failure it exists to prevent.
    """
    msgs = [
        user_msg("go"),
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "write_file", "arguments": json.dumps({
                "file_path": "src/core/llm_client.py",
                "content": "x" * 800,
            })},
        }]},
        tool_result_msg(tid="c1", content="written"),
    ]
    for level in (1, 2, 3):
        new_msgs, _ = cc._compress_region(msgs, budget_tokens=None,
                                          forced_level=level)
        for m in new_msgs:
            for tc in m.get("tool_calls") or []:
                args = tc["function"]["arguments"]
                json.loads(args)          # must not raise


def test_the_useful_part_of_a_tool_call_survives_the_digest():
    """Level 1 shortens the values but keeps the call recognisable."""
    msgs = [
        user_msg("go"),
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "write_file", "arguments": json.dumps({
                "file_path": "src/core/llm_client.py",
                "content": "x" * 800,
            })},
        }]},
        tool_result_msg(tid="c1", content="written"),
    ]
    new_msgs, _ = cc._compress_region(msgs, budget_tokens=None,
                                      forced_level=1)
    calls = [tc for m in new_msgs for tc in (m.get("tool_calls") or [])]
    assert calls, "the tool call should survive at level 1"
    args = json.loads(calls[0]["function"]["arguments"])
    assert "llm_client.py" in args["file_path"]
    assert len(args["content"]) < 800


# ── End to end: the real llm_client seam ───────────────────────────────────

class _FakeMessage:
    """A stand-in for the SDK's message object."""

    def __init__(self, payload):
        self._payload = payload

    def model_dump(self, **_kwargs):
        return dict(self._payload)


class _FakeChoice:
    def __init__(self):
        self.finish_reason = "stop"
        self.message = _FakeMessage({"role": "assistant", "content": "done"})


class _FakeResponse:
    def __init__(self):
        self.choices = [_FakeChoice()]


class _FakeRaw:
    def __init__(self):
        self.text = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "done"}}]})

    def parse(self):
        return _FakeResponse()


class _FakeCompletions:
    def __init__(self, owner):
        self._owner = owner

    @property
    def with_raw_response(self):
        return self

    def create(self, **kwargs):
        self._owner.calls.append(kwargs)
        if self._owner.fail_until > 0:
            self._owner.fail_until -= 1
            raise self._owner.error
        return _FakeRaw()


class _FakeClient:
    def __init__(self, owner):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _FakeCompletions(owner)


class _FakeProvider:
    """Records every request the runtime makes."""

    def __init__(self, fail_until=0, error=None):
        self.calls = []
        self.fail_until = fail_until
        self.error = error or Exception(
            "This model's maximum context length is 8192 tokens")

    def client(self, _pid, _key):
        return _FakeClient(self)


def _install_fake_provider(monkeypatch, provider):
    """
    Route every request to the fake provider.

    The runtime skips a slot when no API key is configured for it, so the key
    resolution is stubbed too — this test is about the context handling, not
    about credentials.
    """
    monkeypatch.setattr(llm_client, "_make_client", provider.client)
    monkeypatch.setattr(llm_client, "_next_key",
                        lambda _pid, _model, _excluded: "test-key")
    llm_client._bad_models.discard(("nvidia", "deepseek-ai/deepseek-v4.1-flash"))


def _wire_is_valid(messages):
    """
    Every assistant tool call must carry parseable JSON arguments, and every
    tool result must have its originating call.  A provider rejects the first
    with a 400 and the second with a 400 too — either one means the
    compressed payload is unusable.
    """
    for m in messages:
        if not isinstance(m, dict):
            return False, "non-dict message"
        for tc in m.get("tool_calls") or []:
            args = tc.get("function", {}).get("arguments", "")
            if not isinstance(args, str):
                return False, "arguments is not a string"
            try:
                json.loads(args)
            except ValueError:
                return False, f"arguments is not valid JSON: {args[:80]!r}"
    pending = set()
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                if tc.get("id"):
                    pending.add(tc["id"])
        elif m.get("role") == "tool":
            if m.get("tool_call_id") not in pending:
                return False, "orphan tool message"
            pending.discard(m["tool_call_id"])
    return True, ""


def test_a_context_error_compresses_and_retries_the_same_model(archive,
                                                                monkeypatch):
    """
    The whole point of the feature: a context error must NOT hand the work to
    another model.  The old passes are compressed, the raw is archived, and
    the same model is retried.
    """
    provider = _FakeProvider(fail_until=1)
    _install_fake_provider(monkeypatch, provider)

    big = "X" * 4000
    history = []
    for i in range(3):
        history.append({"role": "user", "content": f"q{i} {big}"})
        history.append({
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": f"c{i}", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps(
                    {"path": "src/core/llm_client.py", "data": big})},
            }],
        })
        history.append({"role": "tool", "tool_call_id": f"c{i}",
                        "content": big})
        history.append({"role": "assistant", "content": f"a{i} {big}"})

    slots = [{"provider_id": "nvidia",
              "name": "deepseek-ai/deepseek-v4.1-flash",
              "max_tokens": 4096, "enable_thinking": True}]

    reply = llm_client._ask_with_slots(
        "THE CURRENT PROMPT", history, False, "SYSTEM PROMPT", slots)

    assert reply == "done"
    assert len(provider.calls) == 2, "expected one retry after compression"

    # The same model handled both requests — no hand-off.
    assert provider.calls[0]["model"] == provider.calls[1]["model"]

    first = provider.calls[0]["messages"]
    second = provider.calls[1]["messages"]
    # The retry carries less content, even though the marker adds a message.
    assert len(json.dumps(second)) < len(json.dumps(first))

    # The compressed payload is still a valid request.
    ok, why = _wire_is_valid(second)
    assert ok, why

    # The prompt being answered survived.
    assert any(m.get("content") == "THE CURRENT PROMPT" for m in second)

    # The marker points at the archive.
    markers = [m.get("content", "") for m in second
               if m.get("role") == "system"
               and "Compressed context" in str(m.get("content", ""))]
    assert markers, "the compression marker should be in the retry payload"

    # And the raw work is retrievable through the normal tool dispatch.
    import re
    aid = re.search(r"Compressed context (A\d+)", markers[0]).group(1)
    out = json.loads(llm_client._dispatch_tool({
        "function": {"name": "retrieve_archive",
                     "arguments": json.dumps({"archive_id": aid})},
    }))
    assert out["id"] == aid
    assert out["total_chars"] > 0


def test_the_shared_registry_is_not_mutated_by_a_context_error(archive,
                                                               monkeypatch):
    """A context error must not permanently degrade the model for the process."""
    provider = _FakeProvider(fail_until=1)
    _install_fake_provider(monkeypatch, provider)

    before = {id(s): s.get("max_tokens")
              for s in llm_client.MODEL_SLOTS + llm_client.AGENT_MODEL_SLOTS}
    slots = [{"provider_id": "nvidia",
              "name": "deepseek-ai/deepseek-v4.1-flash",
              "max_tokens": 4096, "enable_thinking": True}]
    llm_client._ask_with_slots("hi", [], False, "SYSTEM", slots)
    after = {id(s): s.get("max_tokens")
             for s in llm_client.MODEL_SLOTS + llm_client.AGENT_MODEL_SLOTS}
    assert before == after


def test_the_payload_is_sent_as_it_is_until_the_provider_says_otherwise(
        archive, monkeypatch):
    """
    There is no local guess any more.  The history goes out unchanged, and
    the compression happens when the provider answers with a context error —
    against the window the provider states in that error.
    """
    provider = _FakeProvider(fail_until=1)
    _install_fake_provider(monkeypatch, provider)

    # ~1.1M characters of history — far past any window.
    block = "Y" * 40_000
    history = []
    for i in range(28):
        history.append({"role": "user", "content": f"q{i} {block}"})
        history.append({
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": f"c{i}", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps(
                    {"path": "src/core/llm_client.py", "data": block})},
            }],
        })
        history.append({"role": "tool", "tool_call_id": f"c{i}",
                        "content": block})
        history.append({"role": "assistant", "content": f"a{i} {block}"})

    slots = [{"provider_id": "nvidia",
              "name": "deepseek-ai/deepseek-v4.1-flash",
              "max_tokens": 4096, "enable_thinking": True}]
    llm_client._ask_with_slots("THE CURRENT PROMPT", history, False,
                                "SYSTEM PROMPT", slots)

    # Two requests: the untouched one, and the compressed retry.
    assert len(provider.calls) == 2
    assert len(json.dumps(provider.calls[0]["messages"])) > len(
        json.dumps(provider.calls[1]["messages"]))

    sent = provider.calls[1]["messages"]
    ok, why = _wire_is_valid(sent)
    assert ok, why
    assert any("Compressed context" in str(m.get("content", ""))
               for m in sent), "the payload should carry a compression marker"
    assert any(m.get("content") == "THE CURRENT PROMPT" for m in sent)

    # The output cap was clamped into the window the provider stated.
    assert provider.calls[1]["max_tokens"] <= 8192 // 4


def test_a_short_history_is_never_compressed(archive, monkeypatch):
    """Nothing is compressed before the provider complains."""
    provider = _FakeProvider(fail_until=0)
    _install_fake_provider(monkeypatch, provider)

    slots = [{"provider_id": "nvidia",
              "name": "deepseek-ai/deepseek-v4.1-flash",
              "max_tokens": 4096, "enable_thinking": True}]
    llm_client._ask_with_slots("THE CURRENT PROMPT",
                                [{"role": "user", "content": "hello"}],
                                False, "SYSTEM PROMPT", slots)

    assert len(provider.calls) == 1
    assert not any("Compressed context" in str(m.get("content", ""))
                   for m in provider.calls[0]["messages"])


def test_a_405_status_triggers_the_compression(archive, monkeypatch):
    """
    Some gateways answer an oversized body with 405 and no usable message.
    The status alone has to be enough to start compressing.
    """
    exc = Exception("Method Not Allowed")
    exc.status_code = 405
    provider = _FakeProvider(fail_until=1, error=exc)
    _install_fake_provider(monkeypatch, provider)

    slots = [{"provider_id": "nvidia",
              "name": "deepseek-ai/deepseek-v4.1-flash",
              "max_tokens": 4096, "enable_thinking": True}]
    reply = llm_client._ask_with_slots("THE CURRENT PROMPT", [], False,
                                       "SYSTEM PROMPT", slots)

    assert reply == "done"
    assert len(provider.calls) == 2, "the 405 must trigger one compression"


def test_a_model_whose_catalogue_cannot_fit_is_skipped(archive, monkeypatch):
    """
    The tool catalogue cannot be compressed.  When the provider's window is
    smaller than the catalogue, the request can never succeed — the model is
    skipped instead of burning ten retries on it.
    """
    provider = _FakeProvider(fail_until=99)
    _install_fake_provider(monkeypatch, provider)

    slots = [
        {"provider_id": "nvidia", "name": "model-a",
         "max_tokens": 4096, "enable_thinking": True},
        {"provider_id": "nvidia", "name": "model-b",
         "max_tokens": 4096, "enable_thinking": True},
    ]
    reply = llm_client._ask_with_slots(
        "THE CURRENT PROMPT",
        [{"role": "user", "content": "old work " + "Z" * 4000},
         {"role": "assistant", "content": "old answer " + "Z" * 4000}],
        False, "SYSTEM PROMPT", slots)

    assert "unavailable" in reply.lower()
    # Model-a: the original request and one compressed retry.  Model-b: the
    # history was already compressed, so there was nothing left to squeeze
    # and it was skipped straight away.  Three calls in total — the generic
    # error path would have made ten per model.
    assert len(provider.calls) == 3, len(provider.calls)
    per_model: dict = {}
    for call in provider.calls:
        per_model[call["model"]] = per_model.get(call["model"], 0) + 1
    assert all(count <= 2 for count in per_model.values()), per_model


# ── The head/tail boundary ─────────────────────────────────────────────────

def _split_history(live_shape="clean", size=4000):
    """A history plus a live turn, and the boundary between them."""
    big = "Z" * size
    history = [system_msg()]
    for i in range(4):
        history.append(user_msg(f"step {i}"))
        history.append(tool_call_msg(tid=f"c{i}", args=json.dumps(
            {"query": "x" * 400, "path": f"dir{i}"})))
        history.append(tool_result_msg(tid=f"c{i}", content=big))
        history.append(assistant_msg(f"reply {i}"))

    if live_shape == "clean":
        # The production shape: the history ends with the prompt, the live
        # turn is a complete tool call and its result.
        history.append(user_msg("THE PROMPT"))
        live = [tool_call_msg(tid="live", name="read_file"),
                tool_result_msg(tid="live", content=big)]
    else:
        # The history ends mid-pair: the call is in the history, its result
        # lands in the live turn.
        history.append(tool_call_msg(tid="cX", name="read_file",
                                     args=json.dumps({"path": "p", "d": big})))
        live = [tool_result_msg(tid="cX", content=big)]
    return history + live, len(history)


def test_the_budget_is_honoured_at_a_clean_boundary(archive):
    """The payload must actually fit the budget it was given."""
    msgs, head_len = _split_history("clean")
    for budget in (600, 1200, 2000, 5000):
        new, _, report = cc.fit_to_budget(msgs, budget_tokens=budget,
                                          head_len=head_len)
        assert report["compressed"] is True
        assert cc.estimate_tokens(new) <= budget, (
            f"budget {budget} not met: {cc.estimate_tokens(new)}")
        assert cc._pairs_ok(new)
        assert not cc._unanswered_calls(new)


def test_a_tool_call_that_spans_the_boundary_stays_paired(archive):
    """
    A call issued in the history and answered in the live turn must survive
    the compression on both sides — otherwise the provider rejects the
    request with an unanswered tool call.
    """
    msgs, head_len = _split_history("straddling", size=4000)
    for budget in (600, 1200, 2000, 5000):
        new, _, report = cc.fit_to_budget(msgs, budget_tokens=budget,
                                          head_len=head_len)
        assert report["compressed"] is True
        assert cc._pairs_ok(new), f"budget {budget}: orphan tool message"
        assert not cc._unanswered_calls(new), (
            f"budget {budget}: tool call left without its answer")
        # The call itself is still there — only its arguments were shortened.
        ids = [tc.get("id") for m in new for tc in (m.get("tool_calls") or [])]
        assert "cX" in ids


def test_compression_never_leaves_a_tool_call_without_its_answer(archive):
    """
    The invariant the provider cares about: every tool call has a result and
    every result has a call.
    """
    for shape in ("clean", "straddling"):
        msgs, head_len = _split_history(shape)
        new, _, _ = cc.fit_to_budget(msgs, budget_tokens=300,
                                     head_len=head_len)
        assert not (cc._unanswered_calls(new)
                    - cc._unanswered_calls(msgs)), shape


def test_the_compressed_payload_is_always_a_valid_request(archive):
    """Arguments must parse and pairings must hold, at every level."""
    for shape in ("clean", "straddling"):
        msgs, head_len = _split_history(shape)
        for budget in (200, 600, 2000, 20_000):
            new, _, _ = cc.fit_to_budget(msgs, budget_tokens=budget,
                                         head_len=head_len)
            ok, why = _wire_is_valid(new)
            assert ok, f"{shape} @ {budget}: {why}"
