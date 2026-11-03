"""Regression tests for the persistent key-exhaustion cooldown in llm_client.

Bug: _EXHAUSTED_COOLDOWN (30s) was defined but NEVER used.  When a provider+model
hit _SAME_STATUS_THRESHOLD (10) consecutive same-status API errors, the current
key was permanently added to _persistent_exhausted_keys with no expiry.  The only
clearance path was a successful API response (via _reset_error_counters), but a
blocked key can never produce a success — so the key was permanently locked for
the rest of the process.  This especially affected nvidia models (minimax-m3.1-flash,
deepseek-v4.1-flash) which share a single API key: one 429 storm locked the model
forever.

Fix: track exhaustion timestamps in _exhausted_key_timestamps and release keys
from _persistent_exhausted_keys once _EXHAUSTED_COOLDOWN has elapsed.
"""
import time
import core.llm_client as lc

# Short cooldown for test speed
_COOLDOWN_ORIG = lc._EXHAUSTED_COOLDOWN


def _setup():
    """Reset all persistent state and set a short cooldown."""
    lc._EXHAUSTED_COOLDOWN = 2
    lc._bad_models.clear()
    lc._key_cursor.clear()
    lc._consecutive_error_status.clear()
    lc._consecutive_error_count.clear()
    lc._persistent_exhausted_keys.clear()
    lc._exhausted_key_timestamps.clear()
    lc.API_KEYS = {"nvidia": ["key-1"]}


def _teardown():
    lc._EXHAUSTED_COOLDOWN = _COOLDOWN_ORIG


def test_key_exhausted_then_recovered_after_cooldown():
    """A key locked via _persistent_exhausted_keys must become available again
    after _EXHAUSTED_COOLDOWN seconds elapse."""
    _setup()
    try:
        pid, model = "nvidia", "minimaxai/minimax-m3.1-flash"
        key = "key-1"

        # Simulate the key being exhausted (as the error handler does)
        lc._persistent_exhausted_keys[(pid, model)] = {key}
        lc._exhausted_key_timestamps[(pid, model)] = {key: time.time()}

        # Immediately after: key should still be exhausted
        excluded = lc._get_exhausted_keys(pid, model, {})
        assert key in excluded, "Key should still be exhausted immediately after lockout"

        # Wait for cooldown
        time.sleep(lc._EXHAUSTED_COOLDOWN + 0.5)

        # After cooldown: key should be released and removed from persistent set
        excluded = lc._get_exhausted_keys(pid, model, {})
        assert key not in excluded, "Key should be released after cooldown expires"
        assert (pid, model) not in lc._persistent_exhausted_keys, \
            "Exhausted-key entry should be cleaned up after cooldown"
        assert (pid, model) not in lc._exhausted_key_timestamps, \
            "Timestamp entry should be cleaned up after cooldown"
    finally:
        _teardown()


def test_reset_error_counters_clears_timestamps():
    """A successful response (_reset_error_counters) must clear timestamps."""
    _setup()
    try:
        pid, model = "nvidia", "deepseek-ai/deepseek-v4.1-flash"
        key = "key-1"

        lc._persistent_exhausted_keys[(pid, model)] = {key}
        lc._exhausted_key_timestamps[(pid, model)] = {key: time.time()}

        # Simulate success → counters reset
        lc._reset_error_counters(pid, model)

        assert (pid, model) not in lc._persistent_exhausted_keys
        assert (pid, model) not in lc._exhausted_key_timestamps
    finally:
        _teardown()


def test_next_key_skips_exhausted_but_restores_after_cooldown():
    """_next_key must skip an exhausted key, but after cooldown it must return it."""
    _setup()
    try:
        pid, model = "nvidia", "minimaxai/minimax-m3.1-flash"
        key = "key-1"

        # Exhaust the key
        lc._persistent_exhausted_keys[(pid, model)] = {key}
        lc._exhausted_key_timestamps[(pid, model)] = {key: time.time()}

        # Key should be unavailable right after lockout
        result = lc._next_key(pid, model, lc._get_exhausted_keys(pid, model, {}))
        assert result is None, "Key should be unavailable while exhausted"

        # Wait for cooldown
        time.sleep(lc._EXHAUSTED_COOLDOWN + 0.5)

        # Key should be available again
        result = lc._next_key(pid, model, lc._get_exhausted_keys(pid, model, {}))
        assert result == key, f"Key should be available after cooldown, got {result!r}"
    finally:
        _teardown()


def test_exhausted_key_within_cooldown_stays_locked():
    """A key exhausted for less than _EXHAUSTED_COOLDOWN should stay locked."""
    _setup()
    try:
        pid, model = "nvidia", "minimaxai/minimax-m3.1-flash"
        key = "key-1"

        lc._persistent_exhausted_keys[(pid, model)] = {key}
        old_ts = lc._EXHAUSTED_COOLDOWN
        lc._EXHAUSTED_COOLDOWN = 10  # longer than the sleep
        lc._exhausted_key_timestamps[(pid, model)] = {key: time.time()}

        time.sleep(0.3)

        excluded = lc._get_exhausted_keys(pid, model, {})
        assert key in excluded, "Key should stay locked within cooldown window"
        # Should still be in persistent set (not cleaned up yet)
        assert (pid, model) in lc._persistent_exhausted_keys

        lc._EXHAUSTED_COOLDOWN = old_ts
    finally:
        _teardown()


def test_success_clears_exhaustion_mid_cooldown():
    """Even before cooldown expires, a success should immediately clear the
    exhaustion (preserving existing behaviour)."""
    _setup()
    try:
        pid, model = "nvidia", "deepseek-ai/deepseek-v4.1-flash"
        key = "key-1"

        lc._persistent_exhausted_keys[(pid, model)] = {key}
        lc._exhausted_key_timestamps[(pid, model)] = {key: time.time() - 100}

        # Success BEFORE cooldown would have expired
        lc._reset_error_counters(pid, model)

        excluded = lc._get_exhausted_keys(pid, model, {})
        assert key not in excluded, "Success should clear exhaustion immediately"
        assert (pid, model) not in lc._persistent_exhausted_keys
    finally:
        _teardown()


def test_multiple_keys_partial_exhaustion():
    """When multiple keys exist, an exhausted key is skipped but others are tried."""
    _setup()
    try:
        pid, model = "nvidia", "minimaxai/minimax-m3.1-flash"
        lc.API_KEYS = {"nvidia": ["key-1", "key-2", "key-3"]}

        # Exhaust key-1 only
        lc._persistent_exhausted_keys[(pid, model)] = {"key-1"}
        lc._exhausted_key_timestamps[(pid, model)] = {"key-1": time.time()}

        # _next_key should return one of the non-exhausted keys
        result = lc._next_key(pid, model, lc._get_exhausted_keys(pid, model, {}))
        assert result in ("key-2", "key-3"), f"Should get a non-exhausted key, got {result!r}"
        assert result != "key-1", "Should not return the exhausted key"
    finally:
        _teardown()


def test_local_exhaustion_not_released_by_cooldown():
    """A key exhausted *within this call* (local exhausted_keys) must stay blocked
    even if its persistent timestamp is old enough for cooldown to release it.

    Bug: _get_exhausted_keys used merged.difference_update(expired) which removed
    locally-exhausted keys from the result.  Within a long ask_ai call (>30s with
    multiple exhausted models), a mid-call exhaustion could be re-released by the
    cooldown sweep, causing the key to be retried when it shouldn't be.
    """
    _setup()
    try:
        pid, model = "nvidia", "minimaxai/minimax-m3.1-flash"
        key = "key-1"

        # Key is BOTH locally exhausted (this call) AND persistently exhausted
        # with an old timestamp (cooldown elapsed).
        local = {(pid, model): {key}}
        lc._persistent_exhausted_keys[(pid, model)] = {key}
        lc._exhausted_key_timestamps[(pid, model)] = {key: time.time() - 100}

        result = lc._get_exhausted_keys(pid, model, local)
        assert key in result, (
            "Locally-exhausted key must stay in merged set even when "
            "persistent cooldown has elapsed"
        )
        assert key not in lc._persistent_exhausted_keys.get((pid, model), set()), (
            "Persistent timestamp should still be swept on cooldown expiry"
        )
    finally:
        _teardown()


# --- Duplicate reasoning key regression tests ---

def test_msg_to_dict_no_duplicate_reasoning_content():
    """_msg_to_dict must not produce a dict with both 'reasoning' and
    'reasoning_content' keys.  The OpenAI SDK v2.53 stores 'reasoning_content'
    in model_extra and model_dump() copies it into the dict; adding a separate
    'reasoning' key creates a duplicate that nvidia rejects with a 400
    ('duplicate field `reasoning_content`').
    """
    from openai.types.chat.chat_completion_message import ChatCompletionMessage
    from llm_client import _msg_to_dict

    # nvidia-style response: only reasoning_content
    msg = ChatCompletionMessage(**{
        "role": "assistant", "content": "hello", "reasoning_content": "cot"
    })
    d = _msg_to_dict(msg)
    assert "reasoning" not in d
    assert "reasoning_content" in d
    assert d["reasoning_content"] == "cot"


def test_msg_to_dict_normalizes_reasoning_to_content():
    """If the SDK message has a 'reasoning' field but no 'reasoning_content',
    _msg_to_dict should still normalise to 'reasoning_content' (never 'reasoning').
    """
    from openai.types.chat.chat_completion_message import ChatCompletionMessage
    from llm_client import _msg_to_dict

    msg = ChatCompletionMessage(**{
        "role": "assistant", "content": "hi", "reasoning": "thoughts"
    })
    d = _msg_to_dict(msg)
    assert "reasoning" not in d
    assert "reasoning_content" in d
    assert d["reasoning_content"] == "thoughts"


def test_msg_to_dict_dict_with_both_keys():
    """A dict message that already carries both 'reasoning' and
    'reasoning_content' should be collapsed to one key.
    """
    from llm_client import _msg_to_dict

    d = _msg_to_dict({
        "role": "assistant", "content": "x",
        "reasoning": "r", "reasoning_content": "rc",
    })
    assert not ("reasoning" in d and "reasoning_content" in d)
    assert "reasoning_content" in d


def test_normalize_reasoning_key():
    """_normalize_reasoning_key collapses both keys to a single one."""
    from llm_client import _normalize_reasoning_key

    # Both present, different values
    d = {"reasoning": "r", "reasoning_content": "rc"}
    _normalize_reasoning_key(d)
    assert "reasoning" not in d
    assert d["reasoning_content"] == "rc"

    # Both present, same value
    d2 = {"reasoning": "same", "reasoning_content": "same"}
    _normalize_reasoning_key(d2)
    assert "reasoning" not in d2
    assert d2["reasoning_content"] == "same"

    # Only reasoning present
    d3 = {"reasoning": "r"}
    _normalize_reasoning_key(d3)
    assert "reasoning" not in d3
    assert d3["reasoning_content"] == "r"


def test_sanitize_no_duplicate_in_sanitized():
    """_sanitize_messages_for_provider must not copy both 'reasoning' and
    'reasoning_content' into the cleaned message dict.
    """
    from llm_client import _sanitize_messages_for_provider

    m = {
        "role": "assistant", "content": "ans",
        "reasoning": "r", "reasoning_content": "rc",
        "reasoning_details": [{"text": "det"}],
        "_reasoning_provider": "nvidia",
        "_reasoning_model": "nvidia/minimaxai/minimax-m3.1-flash",
    }
    result = _sanitize_messages_for_provider(
        [m], "nvidia", model_name="nvidia/minimaxai/minimax-m3.1-flash",
        preserve_native_reasoning=True,
    )
    clean = result[0]
    assert not ("reasoning" in clean and "reasoning_content" in clean)
    assert "reasoning_content" in clean
    assert "reasoning_details" in clean


def test_full_nvidia_reasoning_flow_no_duplicate():
    """End-to-end: simulate a nvidia API response → _msg_to_dict → add
    provenance → _sanitize_messages_for_provider.  The final dict sent to
    the API must never have both 'reasoning' and 'reasoning_content'.
    """
    import json as _json
    from openai.types.chat.chat_completion_message import ChatCompletionMessage
    from llm_client import _msg_to_dict, _sanitize_messages_for_provider

    # Step 1: nvidia returns reasoning_content
    msg = ChatCompletionMessage(**{
        "role": "assistant", "content": "Answer", "reasoning_content": "cot"
    })
    d = _msg_to_dict(msg)
    assert "reasoning" not in d

    # Step 2: _ask_with_slots adds provenance (lines 1283-1284)
    d["_reasoning_provider"] = "nvidia"
    d["_reasoning_model"] = "nvidia/minimaxai/minimax-m3.1-flash"

    # Step 3: On the next call, sanitise for the same model
    sanitized = _sanitize_messages_for_provider(
        [d], "nvidia", model_name="nvidia/minimaxai/minimax-m3.1-flash",
        preserve_native_reasoning=True,
    )
    clean = sanitized[0]
    assert not ("reasoning" in clean and "reasoning_content" in clean)

    # Step 4: Verify the JSON body that would be sent
    body = _json.dumps(clean)
    assert "reasoning" not in body or "reasoning_content" not in body or \
        not (body.count('"reasoning"') > 0 and body.count('"reasoning_content"') > 0)
