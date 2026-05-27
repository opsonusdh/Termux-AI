import os
import time
import json
from openai import OpenAI

from prompt import SYSTEM_PROMPT
from renderer import RED, YELLOW, RESET
from tools import *

AI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


PROVIDERS: dict[str, dict] = {
    "google": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1/",
    },
}


MODEL_SLOTS: list[dict] = [
    {"provider_id": "google",  "name": "gemini-3.5-flash",                     "max_tokens": None},
    {"provider_id": "google",  "name": "gemini-3.1-flash-lite",                "max_tokens": None},
    {"provider_id": "google",  "name": "gemini-3-flash-preview",               "max_tokens": None},
    {"provider_id": "groq",    "name": "groq/compound",                        "max_tokens": 8192},
    {"provider_id": "nvidia",  "name": "openai/gpt-oss-120b",                  "max_tokens": 4096},
    {"provider_id": "nvidia",  "name": "deepseek-ai/deepseek-v4-flash",        "max_tokens": 4096},
    {"provider_id": "nvidia",  "name": "nvidia/llama-3.1-nemotron-nano-8b-v1", "max_tokens": 4096},
    {"provider_id": "nvidia",  "name": "deepseek-ai/deepseek-r1",              "max_tokens": 4096},
    {"provider_id": "google",  "name": "gemma-4-31b-it",                       "max_tokens": None},
    {"provider_id": "google",  "name": "gemini-2.5-flash",                     "max_tokens": None},
    {"provider_id": "google",  "name": "gemini-2.5-flash-lite",                "max_tokens": None},
]


def _load_api_keys() -> dict[str, list[str]]:
    path = os.path.join(AI_ROOT, "api.keys")
    raw  = open(path, "r", encoding="utf-8").read().strip()
    try:
        data = json.loads(raw)
        return {k: (v if isinstance(v, list) else [v]) for k, v in data.items()}
    except json.JSONDecodeError:
        keys = [line.strip() for line in raw.splitlines() if line.strip()]
        print(f"{YELLOW}[WARN] api.keys is legacy plain-text. {RESET}")
        return {"google": keys}


API_KEYS: dict[str, list[str]] = _load_api_keys()


def _warn_missing_keys() -> None:
    seen: set[str] = set()
    for slot in MODEL_SLOTS:
        pid = slot["provider_id"]
        if pid not in seen:
            seen.add(pid)
            keys = API_KEYS.get(pid, [])
            if not keys:
                print(
                    f"{YELLOW}[WARN] Provider '{pid}' has no API keys in api.keys. "
                    f"All its slots will be skipped.{RESET}"
                )

_warn_missing_keys()

_key_cursor: dict[str, int]      = {}
_bad_keys:   dict[str, set[str]] = {}


def _next_key(provider_id: str) -> str | None:
    keys = API_KEYS.get(provider_id, [])
    if not keys:
        return None
    bad       = _bad_keys.get(provider_id, set())
    available = [k for k in keys if k not in bad]
    if not available:
        return None
    idx = _key_cursor.get(provider_id, 0) % len(available)
    _key_cursor[provider_id] = idx + 1
    return available[idx]


def _available_key_count(provider_id: str) -> int:
    keys = API_KEYS.get(provider_id, [])
    bad  = _bad_keys.get(provider_id, set())
    return len([k for k in keys if k not in bad])


def _mark_bad(provider_id: str, key: str) -> None:
    _bad_keys.setdefault(provider_id, set()).add(key)


def _reset_provider(provider_id: str) -> None:
    _bad_keys[provider_id]   = set()
    _key_cursor[provider_id] = 0


def _msg_to_dict(msg) -> dict:
    """
    Convert a ChatCompletionMessage object to a plain dict.

    Gemini thinking models attach extra_content.google.thought_signature to
    every tool call. It must be round-tripped back exactly or Gemini 3 returns
    a 400 error.

    The OpenAI SDK stores non-standard API fields in model_extra (Pydantic v2),
    NOT as direct attributes. We check three locations in priority order:
      1. tc.extra_content         — direct attribute (some SDK versions)
      2. tc.model_extra           — Pydantic v2 extra fields dict
      3. tc.model_dump()          — full serialisation, catches everything else
    """
    if isinstance(msg, dict):
        return msg

    # model_dump() is the most complete — use it as primary approach.
    # It serialises ALL fields including Pydantic extra fields (model_extra),
    # so extra_content is preserved automatically if the SDK captured it.
    if hasattr(msg, "model_dump"):
        try:
            return msg.model_dump(exclude_none=True)
        except Exception:
            pass

    # Manual fallback for non-Pydantic objects
    d: dict = {"role": getattr(msg, "role", "assistant")}

    content = getattr(msg, "content", None)
    if content is not None:
        d["content"] = content

    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        serialised = []
        for tc in tool_calls:
            tc_dict: dict = {
                "id":       tc.id,
                "type":     "function",
                "function": {
                    "name":      tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }

            # Check all known locations for extra_content
            extra = getattr(tc, "extra_content", None)
            if extra is None and hasattr(tc, "model_extra"):
                extra = (tc.model_extra or {}).get("extra_content")
            if extra is not None:
                if isinstance(extra, dict):
                    tc_dict["extra_content"] = extra
                elif hasattr(extra, "model_dump"):
                    tc_dict["extra_content"] = extra.model_dump(exclude_none=True)
                elif hasattr(extra, "__dict__"):
                    tc_dict["extra_content"] = extra.__dict__

            serialised.append(tc_dict)
        d["tool_calls"] = serialised

    return d


# Dummy signature accepted by Google to skip validation when history
# originates from a non-Gemini model that never produced real signatures.
_DUMMY_SIG = "context_engineering_is_the_way_to_go"

_GEMINI_PROVIDERS = {"google"}   # providers that require thought_signatures


def _sanitize_messages_for_provider(messages: list[dict], pid: str) -> list[dict]:
    """
    Adapt message history when switching between providers mid-fallback.

    Gemini → non-Gemini : strip extra_content (Nvidia/Groq don't understand it)
    non-Gemini → Gemini  : inject dummy thought_signature so Gemini 3 doesn't 400
    Same provider        : return as-is
    """
    going_to_gemini = pid in _GEMINI_PROVIDERS
    result = []

    for m in messages:
        if not isinstance(m, dict) or "tool_calls" not in m:
            result.append(m)
            continue

        new_tcs = []
        for i, tc in enumerate(m["tool_calls"]):
            tc = dict(tc)  # shallow copy — don't mutate original
            has_sig = (
                isinstance(tc.get("extra_content"), dict)
                and tc["extra_content"].get("google", {}).get("thought_signature")
            )

            if going_to_gemini and not has_sig:
                # Inject dummy signature on the first tool call of each step.
                # Subsequent parallel calls in the same message don't need one.
                if i == 0:
                    tc["extra_content"] = {"google": {"thought_signature": _DUMMY_SIG}}

            elif not going_to_gemini and has_sig:
                # Strip Gemini-specific extra_content — other providers reject it
                tc.pop("extra_content", None)

            new_tcs.append(tc)

        result.append({**m, "tool_calls": new_tcs})

    return result


def _make_client(provider_id: str, api_key: str | None) -> OpenAI:
    return OpenAI(
        api_key  = api_key or "no-key",
        base_url = PROVIDERS[provider_id]["base_url"],
    )


def _stitch_assistant_turns(messages: list[dict], last_chunk: str) -> str:
    parts = [
        m["content"]
        for m in messages
        if m.get("role") == "assistant"
        and isinstance(m.get("content"), str)
        and m["content"].strip()
    ]
    parts.append(last_chunk)
    return "".join(parts).strip()


def _dispatch_tool(tool_call: dict, voice: bool = False) -> str:
    name     = tool_call["function"]["name"]
    raw_args = tool_call["function"].get("arguments", "{}")
    try:
        args = json.loads(raw_args or "{}")
    except Exception:
        args = {}

    g = args.get
    routes = {
        "run_code":        lambda: run_code(
                               bash    = g("bash", ""),
                               timeout = int(g("timeout", 0)),
                           ),
        "save_memory":     lambda: save_memory(
                               text     = g("text", ""),
                               type_    = g("type_", "fact"),
                               tags     = g("tags", ""),
                               priority = int(g("priority", 7)),
                           ),
        "retrieve_memory": lambda: retrieve_memory(
                               query = g("query", ""),
                               top_k = int(g("top_k", 5)),
                           ),
        "read_file":       lambda: read_file(
                               path          = g("path", ""),
                               segment_start = g("segment_start"),
                               segment_end   = g("segment_end"),
                               unit          = g("unit", "lines"),
                           ),
        "write_file":      lambda: write_file(
                               path          = g("path", ""),
                               content       = g("content", ""),
                               mode          = g("mode", "overwrite"),
                               segment_start = g("segment_start"),
                               segment_end   = g("segment_end"),
                               unit          = g("unit", "lines"),
                           ),
        "index_files":     lambda: index_files(
                               path             = g("path", ""),
                               extension_filter = g("extension_filter", ""),
                           ),
        "web_scrape":      lambda: web_scrape(
                               url      = g("url", ""),
                               selector = g("selector", None),
                           ),
        "sleep_mode":      lambda: sleep_mode(),
        "intermediate_print": lambda: intermediate_print(
                                text  = g("text", ""),
                                voice = voice,
                            ),
    }

    fn = routes.get(name)
    if fn:
        return fn()
    print(f"{RED}[ERROR] Unknown tool: {name}{RESET}")
    return f"[ERROR] Unknown tool: {name}"


_EXHAUSTED_COOLDOWN = 30

# Set to True to enable verbose debug output
DEBUG = False

def _dbg(*args) -> None:
    if DEBUG:
        print(f"[DEBUG]", *args)


def ask_ai(prompt: str, history: list[dict] | None = None, voice: bool = False) -> str:
    memory_block   = build_memory_block(prompt)
    system_content = (memory_block + "\n\n" + SYSTEM_PROMPT) if memory_block else SYSTEM_PROMPT

    base_messages: list[dict] = [{"role": "system", "content": system_content}]
    if history:
        base_messages.extend(history)
    base_messages.append({"role": "user", "content": prompt})

    slot      = 0
    last_slot = -1   # track slot changes to know when to reset messages
    messages  = []   # preserved across key-retries within the same slot

    while True:
        if slot >= len(MODEL_SLOTS):
            print(
                f"{YELLOW}[WARN] All models exhausted. "
                f"Resetting and retrying from top in {_EXHAUSTED_COOLDOWN} s...{RESET}"
            )
            time.sleep(_EXHAUSTED_COOLDOWN)
            for pid in API_KEYS:
                _reset_provider(pid)
            slot      = 0
            last_slot = -1
            messages  = []

        slot_cfg   = MODEL_SLOTS[slot]
        pid        = slot_cfg["provider_id"]
        model_name = slot_cfg["name"]
        max_tok    = slot_cfg["max_tokens"]

        if not API_KEYS.get(pid):
            print(
                f"{RED}[SKIP] '{model_name}' — provider '{pid}' has no API keys "
                f"in api.keys.{RESET}"
            )
            slot += 1
            continue

        api_key = _next_key(pid)

        if api_key is None:
            print(f"{RED}[{pid}] All keys exhausted — skipping '{model_name}'.{RESET}")
            slot += 1
            continue

        # FIX: only reset messages when slot actually changes.
        # Previously messages were reset on EVERY outer loop iteration,
        # including key-retries within the same slot — losing tool call history.
        if slot != last_slot:
            messages  = _sanitize_messages_for_provider(list(base_messages), pid)
            last_slot = slot
            base_len  = len(messages)
            _dbg(f"Slot changed → [{pid}/{model_name}]. Messages reset. "
                 f"History depth: {len(messages)}")
        else:
            _dbg(f"Key retry on [{pid}/{model_name}]. "
                 f"Preserving {len(messages)} messages.")

        client = _make_client(pid, api_key)

        try:
            while True:
                kwargs: dict = dict(
                    model       = model_name,
                    messages    = messages,
                    tools       = TOOLS_DESCRIPTION,
                    tool_choice = "auto",
                )
                if max_tok is not None:
                    kwargs["max_tokens"] = max_tok

                _dbg(f"→ API call [{pid}/{model_name}] | "
                     f"messages: {len(messages)} | "
                     f"max_tokens: {max_tok}")

                # Use with_raw_response to capture the raw JSON BEFORE the SDK
                # parses it. The OpenAI SDK Pydantic models silently drop unknown
                # fields like extra_content (which carries thought_signature).
                # model_dump() / getattr cannot recover what was already thrown away.
                raw           = client.chat.completions.with_raw_response.create(**kwargs)
                response      = raw.parse()
                raw_json      = json.loads(raw.text)

                choice        = response.choices[0]
                finish_reason = choice.finish_reason
                msg_dict      = _msg_to_dict(choice.message)

                # Merge extra_content back from raw JSON into each tool call dict.
                # This restores thought_signature that the SDK dropped during parsing.
                if msg_dict.get("tool_calls"):
                    raw_tcs = (
                        raw_json.get("choices", [{}])[0]
                                .get("message", {})
                                .get("tool_calls", [])
                    )
                    for tc_dict, raw_tc in zip(msg_dict["tool_calls"], raw_tcs):
                        raw_extra = raw_tc.get("extra_content")
                        if raw_extra and "extra_content" not in tc_dict:
                            tc_dict["extra_content"] = raw_extra
                            _dbg(f"  extra_content merged for tool call '{tc_dict['function']['name']}'")
                        elif not raw_extra:
                            _dbg(f"  no extra_content in raw JSON for '{tc_dict['function']['name']}'")

                _dbg(f"← finish_reason: {finish_reason} | "
                     f"tool_calls: {len(msg_dict.get('tool_calls') or [])} | "
                     f"content_len: {len(msg_dict.get('content') or '')}")

                if msg_dict.get("tool_calls"):
                    if finish_reason == "length":
                        print(
                            f"{RED}[{pid}/{model_name}] Tool call truncated. "
                            f"Injecting error and retrying.{RESET}"
                        )
                        messages.append(msg_dict)
                        for tc in msg_dict["tool_calls"]:
                            messages.append({
                                "role":         "tool",
                                "tool_call_id": tc["id"],
                                "content": (
                                    "[ERROR] Tool call was cut off by the token limit. "
                                    "Please retry with smaller or fewer arguments."
                                ),
                            })
                        continue

                    messages.append(msg_dict)
                    for tc in msg_dict["tool_calls"]:
                        tool_name   = tc["function"]["name"]
                        tool_result = _dispatch_tool(tc, voice=voice)
                        _dbg(f"  tool '{tool_name}' → result len: {len(str(tool_result))}")
                        messages.append({
                            "role":         "tool",
                            "tool_call_id": tc["id"],
                            "content":      tool_result,
                        })
                    continue

                partial = msg_dict.get("content") or ""

                if finish_reason == "length":
                    anchor = partial[-80:].strip() if partial else ""
                    print(
                        f"{RED}[{pid}/{model_name}] Token limit hit. "
                        f"Continuing from: '...{anchor}'{RESET}"
                    )
                    messages.append({"role": "assistant", "content": partial})
                    messages.append({
                        "role":    "user",
                        "content": (
                            "Continue exactly from where you left off. "
                            f"Do not repeat anything. Last words: '...{anchor}'"
                        ),
                    })
                    continue

                return _stitch_assistant_turns(messages[base_len:], partial) or "[EMPTY RESPONSE]"

        except Exception as e:
            s = str(e)

            if any(x in s for x in ("429", "RESOURCE_EXHAUSTED", "rate limit")):
                if api_key:
                    _mark_bad(pid, api_key)
                left = _available_key_count(pid)
                print(
                    f"{RED}[{pid}/{model_name}] Rate-limited. "
                    f"Keys remaining: {left}.{RESET}"
                )
                _dbg(f"  Rate-limit on key ...{api_key[-6:] if api_key else 'none'}. "
                     f"Messages preserved: {len(messages)}. "
                     f"Will retry same slot with next key.")
                time.sleep(3)
                if left == 0:
                    _reset_provider(pid)
                    slot += 1
                    _dbg(f"  All keys exhausted for {pid}. Advancing to slot {slot}.")

            elif any(x in s for x in ("503", "UNAVAILABLE", "overloaded")):
                print(f"{RED}[{pid}/{model_name}] Overloaded. Retrying in 5 s.{RESET}")
                time.sleep(5)

            elif "API_KEY_INVALID" in s:
                print(f"{RED}[{pid}] Invalid API key — skipping '{model_name}'.{RESET}")
                slot += 1

            else:
                _dbg(f"  Unhandled exception: {s[:300]}")
                raise
