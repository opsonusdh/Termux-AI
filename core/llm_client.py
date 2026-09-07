import os
import sys
import time
import json
import re

# Path bootstrap
_CORE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_CORE)
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)
if _ROOT not in sys.path:
    sys.path.insert(1, _ROOT)

import paths
from agent import state_manager
from openai import OpenAI
from core.renderer import RED, YELLOW, RESET, GRAY
from tools import *
import context_manager as _cm
import core.display_state as display_state
from core.models import PROVIDERS, MODEL_SLOTS, AGENT_MODEL_SLOTS

with open(paths.PROMPT_FILE, encoding="utf-8") as file:
    SYSTEM_PROMPT = file.read()


def _load_api_keys() -> dict[str, list[str]]:
    path = paths.API_KEYS_FILE
    res: dict[str, list[str]] = {}

    if os.path.exists(path):
        try:
            raw = open(path, "r", encoding="utf-8").read().strip()
            data = json.loads(raw)
            res = {k: (v if isinstance(v, list) else [v]) for k, v in data.items()}
        except Exception:
            try:
                raw = open(path, "r", encoding="utf-8").read().strip()
                keys = [line.strip() for line in raw.splitlines() if line.strip()]
                print(f"{YELLOW}[WARN] api.keys is legacy plain-text. {RESET}")
                res = {"google": keys}
            except Exception:
                res = {}

    # Fallback to Environment Variables
    env_map = {
        "openrouter": ["OPENROUTER_API_KEY"],
        "google":     ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "groq":       ["GROQ_API_KEY"],
        "nvidia":     ["NVIDIA_API_KEY"],
    }
    for provider, env_vars in env_map.items():
        existing = res.get(provider, [])
        for ev in env_vars:
            val = os.environ.get(ev)
            if val and val not in existing:
                existing.append(val)
        res[provider] = existing

    return res

API_KEYS: dict[str, list[str]] = _load_api_keys()
# Tracks whether we are currently executing an autonomous agent task
_in_agent_mode = False


def _warn_missing_keys() -> None:
    seen: set[str] = set()
    for slot in MODEL_SLOTS:
        pid = slot["provider_id"]
        if pid not in seen:
            seen.add(pid)
            keys = API_KEYS.get(pid, [])
            if not keys:
                print(
                    f"{YELLOW}[WARN] Provider '{pid}' has no API keys in api.keys or environment. "
                    f"All its slots will be skipped.{RESET}"
                )

_warn_missing_keys()

# Availability state is scoped as narrowly as possible.
# Rate limits/transient failures affect one provider + key + model only.
# Invalid credentials disable one key; 404/model-not-found disables one model.
_key_cursor: dict[tuple[str, str], int] = {}
_bad_keys: dict[str, set[str]] = {}
_bad_models: set[tuple[str, str]] = set()
_account_restricted_until: dict[str, float] = {}
_rate_limited_until: dict[tuple[str, str, str], float] = {}
_transient_until: dict[tuple[str, str], float] = {}

_RATE_LIMIT_COOLDOWN = 30.0
_TRANSIENT_COOLDOWN = 5.0
_ACCOUNT_RESTRICTION_COOLDOWN = 300.0


def _next_key(
    provider_id: str,
    model_name: str,
    excluded_keys: set[str] | None = None,
) -> str | None:
    keys = API_KEYS.get(provider_id, [])
    if not keys or (provider_id, model_name) in _bad_models:
        return None

    excluded_keys = excluded_keys or set()
    bad = _bad_keys.get(provider_id, set())

    cursor_key = (provider_id, model_name)
    idx = _key_cursor.get(cursor_key, 0) % len(keys)
    for offset in range(len(keys)):
        key = keys[(idx + offset) % len(keys)]
        if key in bad or key in excluded_keys:
            continue
        _key_cursor[cursor_key] = (keys.index(key) + 1) % len(keys)
        return key
    return None


def _available_key_count(provider_id: str, model_name: str) -> int:
    if (provider_id, model_name) in _bad_models:
        return 0
    if _account_restricted_until.get(provider_id, 0.0) > time.monotonic():
        return 0
    if _transient_until.get((provider_id, model_name), 0.0) > time.monotonic():
        return 0
    keys = API_KEYS.get(provider_id, [])
    bad = _bad_keys.get(provider_id, set())
    now = time.monotonic()
    return sum(
        1
        for key in keys
        if key not in bad
        and _rate_limited_until.get((provider_id, key, model_name), 0.0) <= now
    )


def _mark_bad(provider_id: str, key: str) -> None:
    # Invalid/forbidden credentials are key-wide.
    _bad_keys.setdefault(provider_id, set()).add(key)


def _mark_account_restricted(provider_id: str) -> None:
    # Organization/account restrictions affect every key under this provider.
    _account_restricted_until[provider_id] = (
        time.monotonic() + _ACCOUNT_RESTRICTION_COOLDOWN
    )


def _mark_model_bad(provider_id: str, model_name: str) -> None:
    _bad_models.add((provider_id, model_name))


def _mark_rate_limited(provider_id: str, key: str, model_name: str) -> None:
    _rate_limited_until[(provider_id, key, model_name)] = (
        time.monotonic() + _RATE_LIMIT_COOLDOWN
    )


def _mark_transient(provider_id: str, model_name: str) -> None:
    # 5xx/server overload is normally provider+model scoped, not credential
    # scoped. Skip the remaining keys for this model instead of hammering the
    # same failing endpoint with every credential.
    _transient_until[(provider_id, model_name)] = (
        time.monotonic() + _TRANSIENT_COOLDOWN
    )


def _clear_expired_state() -> None:
    now = time.monotonic()
    for provider_id, until in list(_account_restricted_until.items()):
        if until <= now:
            _account_restricted_until.pop(provider_id, None)
    for store in (_rate_limited_until, _transient_until):
        for identity, until in list(store.items()):
            if until <= now:
                store.pop(identity, None)


def _temporary_retry_delay() -> float | None:
    _clear_expired_state()
    now = time.monotonic()
    deadlines = [
        until
        for until in (*_rate_limited_until.values(), *_transient_until.values())
        if until > now
    ]
    if not deadlines:
        return None
    return max(0.0, min(deadlines) - now)


def _reset_provider(provider_id: str) -> None:
    # Compatibility helper: reset temporary provider state. Permanent failures
    # stay disabled, except account restrictions which are deliberately cooldown-based.
    _account_restricted_until.pop(provider_id, None)
    for key in list(_key_cursor):
        if key[0] == provider_id:
            _key_cursor.pop(key, None)
    for identity in list(_rate_limited_until):
        if identity[0] == provider_id:
            _rate_limited_until.pop(identity, None)
    for identity in list(_transient_until):
        if identity[0] == provider_id:
            _transient_until.pop(identity, None)


def _error_status(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    message = str(exc)
    for code in (400, 401, 403, 404, 429, 500, 502, 503, 504):
        if re.search(rf"\b{code}\b", message):
            return code
    return None


def _is_rate_limit(exc: Exception) -> bool:
    status = _error_status(exc)
    s = str(exc).lower()
    return status == 429 or any(
        x in s for x in ("resource_exhausted", "rate limit", "rate-limit", "too many requests")
    )


def _is_transient(exc: Exception) -> bool:
    status = _error_status(exc)
    s = str(exc).lower()
    return status in {500, 502, 503, 504} or any(
        x in s for x in ("temporarily unavailable", "server error", "overloaded", "unavailable")
    )


def _is_account_restriction(exc: Exception) -> bool:
    """Identify 400-level account/org restrictions that are not model errors."""
    status = _error_status(exc)
    if status != 400:
        return False
    s = str(exc).lower()
    return any(
        x in s
        for x in (
            "organization has been restricted",
            "organization is restricted",
            "organization has been suspended",
            "organization is suspended",
            "account has been restricted",
            "account is restricted",
            "account has been suspended",
            "account is suspended",
            "project has been disabled",
            "project is disabled",
        )
    )


def _is_context_error(exc: Exception) -> bool:
    """Identify request-size/context errors independently of HTTP status."""
    s = str(exc).lower()
    return any(
        x in s
        for x in (
            "context", "token_limit", "max_tokens", "exceed",
            "excluding", "window", "too many tokens", "prompt is too long",
        )
    )


def _is_invalid_request(exc: Exception) -> bool:
    """Identify recoverable 400/422 request-shape errors."""
    status = _error_status(exc)
    if status not in {400, 422}:
        return False
    if _is_account_restriction(exc) or _is_context_error(exc):
        return False
    s = str(exc).lower()
    return any(
        x in s
        for x in (
            "invalid request", "invalid parameter", "invalid value",
            "invalid argument", "malformed", "unsupported field",
            "all elements", "expected", "must be",
        )
    )


def _msg_to_dict(msg) -> dict:
    """Serialize an SDK message while preserving native reasoning metadata."""
    if isinstance(msg, dict):
        d = dict(msg)
    elif hasattr(msg, "model_dump"):
        try:
            d = msg.model_dump(exclude_none=True)
        except Exception:
            d = {"role": getattr(msg, "role", "assistant")}
    else:
        d = {"role": getattr(msg, "role", "assistant")}

    if not isinstance(msg, dict):
        content = getattr(msg, "content", None)
        reasoning = getattr(msg, "reasoning", None) or getattr(
            msg, "reasoning_content", None
        )
        reasoning_details = getattr(msg, "reasoning_details", None)

        if content is not None:
            d["content"] = content
        if reasoning:
            d["reasoning"] = reasoning
        if reasoning_details is not None:
            d["reasoning_details"] = reasoning_details

        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            serialised = []
            for tc in tool_calls:
                tc_dict = {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
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

    if "reasoning_details" not in d and hasattr(msg, "model_extra"):
        extra = getattr(msg, "model_extra", None) or {}
        if isinstance(extra, dict) and "reasoning_details" in extra:
            d["reasoning_details"] = extra["reasoning_details"]

    return d



_DUMMY_SIG = "context_engineering_is_the_way_to_go"

_GEMINI_PROVIDERS = {"google"}


def _sanitize_messages_for_provider(
    messages: list[dict],
    pid: str,
    model_name: str | None = None,
    preserve_native_reasoning: bool = True,
) -> list[dict]:
    going_to_gemini = pid in _GEMINI_PROVIDERS
    result = []

    for m in messages:
        if not isinstance(m, dict):
            continue

        clean_m = {"role": m.get("role", "user")}

        content = m.get("content")
        if isinstance(content, (dict, list)):
            clean_m["content"] = json.dumps(content, ensure_ascii=False)
        elif content is not None:
            clean_m["content"] = content
        else:
            clean_m["content"] = None

        if "tool_call_id" in m:
            clean_m["tool_call_id"] = m["tool_call_id"]
        if "name" in m:
            clean_m["name"] = m["name"]

        if (
            preserve_native_reasoning
            and m.get("_reasoning_provider") == pid
            and m.get("_reasoning_model") == model_name
        ):
            for reasoning_key in ("reasoning", "reasoning_content", "reasoning_details"):
                if reasoning_key in m:
                    clean_m[reasoning_key] = m[reasoning_key]

        if "tool_calls" in m and m["tool_calls"]:
            new_tcs = []
            for i, tc in enumerate(m["tool_calls"]):
                tc = dict(tc)
                clean_tc = {
                    "id": tc.get("id", ""),
                    "type": tc.get("type", "function"),
                    "function": tc.get("function", {}),
                }
                has_sig = (
                    isinstance(tc.get("extra_content"), dict)
                    and tc["extra_content"].get("google", {}).get("thought_signature")
                )

                if going_to_gemini:
                    if has_sig:
                        clean_tc["extra_content"] = tc["extra_content"]
                    elif i == 0:
                        clean_tc["extra_content"] = {"google": {"thought_signature": _DUMMY_SIG}}

                new_tcs.append(clean_tc)

            clean_m["tool_calls"] = new_tcs

        result.append(clean_m)

    return result


def _make_client(provider_id: str, api_key: str | None) -> OpenAI:
    headers = {}
    if provider_id == "openrouter":
        headers = {
            "HTTP-Referer": "https://github.com/opsonusdh/Termux-AI",
            "X-Title": "Termux-AI",
        }
    return OpenAI(
        api_key         = api_key or "no-key",
        base_url        = PROVIDERS[provider_id]["base_url"],
        default_headers = headers if headers else None,
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


_TOOL_TAGS = {
    "run_code":    "EXEC",
    "write_file":  "EDITING FILE",
    "read_file":   "READING FILE",
    "find_replace": "FIND REPLACE",
    "manage_todos": "TODO",
}

# Individual lines longer than this in a *collapsed* command preview get cut
# off — "first three lines" is meant to stay a brief preview even if one of
# those lines is itself huge (e.g. a minified one-liner). Expanded mode is
# never capped: that mode's whole point is to show everything.
_COLLAPSED_LINE_CHAR_CAP = 200


def _tool_header_lines(name: str, args: dict, expanded: bool) -> list[str]:
    """Build the tag/preview line(s) printed before [OUTPUT]/[TOOL DONE] for
    one tool call. Presentation-layer only — never touches the tool's actual
    return value, only what gets echoed to the terminal.
    """
    tag = _TOOL_TAGS.get(name, name.upper())

    if name == "run_code":
        bash = str(args.get("bash", "") or "")
        lines = bash.splitlines() or [""]
        if expanded:
            body = bash
        else:
            preview_lines = lines[:3]
            preview_lines = [
                (l if len(l) <= _COLLAPSED_LINE_CHAR_CAP else l[: _COLLAPSED_LINE_CHAR_CAP - 3] + "...")
                for l in preview_lines
            ]
            body = "\n".join(preview_lines)
            if len(lines) > 3:
                body += "\n..."
        return [f"[{tag}]", body]

    if name in ("write_file", "read_file"):
        if not expanded:
            return [f"[{tag}]"]
        path = args.get("path", "")
        seg_start = args.get("segment_start")
        seg_end = args.get("segment_end")
        unit = args.get("unit", "lines") or "lines"
        span = f"{seg_start}-{seg_end}" if (seg_start is not None or seg_end is not None) else "all"
        if name == "write_file":
            mode = args.get("mode", "overwrite")
            return [f"[{tag}] {path} | mode: {mode} | {unit}: {span}"]
        return [f"[{tag}] {path} | {unit}: {span}"]

    if name == "find_replace":
        if not expanded:
            return [f"[{tag}]"]
        path = args.get("path", "")
        find_text = args.get("find", "")
        segment_start = args.get("segment_start")
        segment_end = args.get("segment_end")
        # Show truncated find text in header
        find_preview = find_text[:80] + ("..." if len(find_text) > 80 else "")
        find_preview = find_preview.replace(chr(10), "\n")
        if segment_start is not None or segment_end is not None:
            seg_desc = f" lines {segment_start or 1}..{segment_end or "end"}"
            return [f"[{tag}] {path}{seg_desc} | find: {find_preview}"]
        return [f"[{tag}] {path} | find: {find_preview}"]
        return [f"[{tag}] {path} | find: {find_preview}"]

    # Every other tool: unchanged detail-picking, restructured into the same
    # tag(+detail)-then-OUTPUT-then-TOOL DONE shape as EXEC/EDITING FILE.
    if not expanded:
        return [f"[{tag}]"]
    detail = ""
    preferred = ("path", "bash", "query", "url", "to", "jid", "filename", "prompt")
    for key in preferred:
        value = args.get(key)
        if value not in (None, ""):
            detail = str(value)
            break
    return [f"[{tag}]{' ' + detail if detail else ''}"]


def _dispatch_tool(tool_call: dict, voice: bool = False) -> str:
    name     = tool_call["function"]["name"]
    raw_args = tool_call["function"].get("arguments", "{}")
    try:
        args = json.loads(raw_args or "{}")
    except Exception:
        args = {}

    g = args.get
    started = time.monotonic()
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
        "find_replace":    lambda: find_replace(
                                path           = g("path", ""),
                                find           = g("find", ""),
                                replace        = g("replace", ""),
                                case_sensitive = g("case_sensitive", True),
                           ),
        "index_files":     lambda: index_files(
                                path             = g("path", ""),
                                extension_filter = g("extension_filter", ""),
                           ),
        "web_scrape":      lambda: web_scrape(
                                url      = g("url", ""),
                                selector = g("selector", None),
                                max_chars = int(g("max_chars", 12000)),
                           ),
        "sleep_mode":      lambda: sleep_mode(),
        "intermediate_print": lambda: intermediate_print(
                                text  = g("text", ""),
                                voice = voice,
                            ),
        "send_whatsapp_message": lambda: send_whatsapp_message(
                                to_phone     = g("to_phone", ""),
                                message_text = g("message_text", ""),
                            ),
        "get_whatsapp_status": lambda: get_whatsapp_status(),
        "get_whatsapp_chats": lambda: get_whatsapp_chats(
                                filter_type  = g("filter_type", "all")
                            ),
        "get_pending_whatsapp_messages": lambda: get_pending_whatsapp_messages(
                                clear        = _safe_bool(g("clear", True)),
                            ),
        "fetch_whatsapp_chat_history": lambda: fetch_whatsapp_chat_history(
                                to_phone     = g("to_phone", ""),
                                limit        = int(g("limit", 5)),
                            ),
        "set_whatsapp_busy_mode": lambda: set_whatsapp_busy_mode(
                                enabled      = _safe_bool(g("enabled", False)),
                                instruction  = g("instruction", ""),
                                exclude_all_groups_except = g("exclude_all_groups_except", None)
                            ),
        "get_whatsapp_report": lambda: get_whatsapp_report(
                                clear        = _safe_bool(g("clear", False)),
                            ),
        "set_whatsapp_user_profile": lambda: set_whatsapp_user_profile(
                                profile      = g("profile", ""),
                            ),
        "initialize_project": lambda: json.dumps(state_manager.initialize_project(
                                 name = g("name", ""),
                                 goal = g("goal", ""),
                             )),
        "add_subtask":     lambda: state_manager.add_subtask(
                                 description = g("description", ""),
                             ),
        "update_subtask":  lambda: state_manager.update_subtask(
                                 task_id      = int(g("task_id", 0)),
                                 status       = g("status"),
                                 notes        = g("notes"),
                                 verification = g("verification"),
                             ),
        "retrieve_chunk":  lambda: _cm.retrieve_chunk(
                                 chunk_id = g("chunk_id", 0),
                             ),
        "list_chunks":     lambda: _cm.list_chunks(),
        "run_diagnosis":   lambda: run_diagnosis(),
        "silence_whatsapp_contact": lambda: silence_whatsapp_contact(
                                jid   = g("jid",   ""),
                                hours = float(g("hours", 24)),
                            ),
        "react_to_whatsapp_message": lambda: react_to_whatsapp_message(
                                message_id = g("message_id", ""),
                                emoji      = g("emoji", ""),
                            ),
        "get_whatsapp_contact_info": lambda: get_whatsapp_contact_info(
                                jid = g("jid", ""),
                            ),
        "get_whatsapp_group_participants": lambda: get_whatsapp_group_participants(
                                jid = g("jid", ""),
                            ),
        "download_whatsapp_media": lambda: download_whatsapp_media(
                                message_id = g("message_id", ""),
                            ),
        "schedule_whatsapp_message": lambda: schedule_whatsapp_message(
                                to      = g("to", ""),
                                message = g("message", ""),
                                send_at = g("send_at", ""),
                            ),
        "search_whatsapp_chat": lambda: search_whatsapp_chat(
                                jid   = g("jid", ""),
                                query = g("query", ""),
                                limit = int(g("limit", 20)),
                            ),
        "archive_whatsapp_chat": lambda: archive_whatsapp_chat(
                                jid     = g("jid", ""),
                                archive = _safe_bool(g("archive", True)),
                            ),
        "set_whatsapp_seen": lambda: set_whatsapp_seen(
                                jid = g("jid", ""),
                            ),
        "delegate_subtask":  lambda: delegate_subtask(
                                task       = g("task", ""),
                                context    = g("context", ""),
                                model      = g("model", "openai/gpt-oss-120b"),
                                max_tokens = int(g("max_tokens", 2048)),
                             ),
        "generate_image":    lambda: generate_image(
                                prompt   = g("prompt", ""),
                                quality  = g("quality", "flash"),
                                filename = g("filename", None),
                             ),
        "list_directory":    lambda: list_directory(
                                path      = g("path", "."),
                                recursive = _safe_bool(g("recursive", False)),
                                max_depth = int(g("max_depth", 3)),
                             ),
        "search_in_files":  lambda: search_in_files(
                                path             = g("path", "."),
                                query            = g("query", ""),
                                file_pattern     = g("file_pattern", "*"),
                                case_sensitive   = _safe_bool(g("case_sensitive", False)),
                                max_results      = int(g("max_results", 50)),
                                max_file_size_kb = int(g("max_file_size_kb", 2048)),
                             ),
        "search_files":      lambda: search_files(
                                path        = g("path", "."),
                                pattern     = g("pattern", ""),
                                file_type   = g("file_type", "all"),
                                max_results = int(g("max_results", 50)),
                             ),
        "rename_file":       lambda: rename_file(
                                old_path = g("old_path", ""),
                                new_path = g("new_path", ""),
                             ),
        "delete_file":       lambda: delete_file(
                                path  = g("path", ""),
                                force = _safe_bool(g("force", False)),
                             ),
        "http_request":      lambda: http_request(
                                url     = g("url", ""),
                                method  = g("method", "GET"),
                                headers = g("headers", None),
                                body    = g("body", None),
                                timeout = int(g("timeout", 15)),
                             ),
        "get_datetime":      lambda: get_datetime(),
        "get_contacts":      lambda: get_contacts(),
        "get_contact_by_id": lambda: get_contact_by_id(
                                contact_id = g("contact_id", ""),
                             ),
        "list_sensors":      lambda: list_sensors(),
        "get_sensor_data":   lambda: get_sensor_data(
                                sensor_type = g("sensor_type", "accelerometer"),
                                samples     = int(g("samples", 1)),
                                delay       = int(g("delay", 100)),
                             ),
        "set_wallpaper":     lambda: set_wallpaper(
                                image_path = g("image_path", ""),
                                lockscreen = _safe_bool(g("lockscreen", False)),
                             ),
        "take_photo":        lambda: take_photo(
                                output_path = g("output_path", None),
                                camera_id   = int(g("camera_id", 0)),
                             ),
        "record_video":      lambda: record_video(
                                output_path = g("output_path", None),
                                camera_id   = int(g("camera_id", 0)),
                                limit       = int(g("limit", 0)),
                             ),
        "record_audio":      lambda: record_audio(
                                output_path = g("output_path", None),
                                format      = g("format", "aac"),
                                bitrate     = int(g("bitrate", 128000)),
                                duration    = int(g("duration", 10)),
                             ),
        "pick_file":         lambda: pick_file(
                                multiple   = _safe_bool(g("multiple", False)),
                                mime_type  = g("mime_type", "*/*"),
                             ),
        "share_file":        lambda: share_file(
                                file_path  = g("file_path", ""),
                                mime_type  = g("mime_type", None),
                                subject    = g("subject", None),
                                text       = g("text", None),
                             ),
        "share_text":        lambda: share_text(
                                text    = g("text", ""),
                                subject = g("subject", None),
                             ),
        "manage_todos":      lambda: manage_todos(
                                action  = g("action", ""),
                                title   = g("title", None),
                                tasks   = g("tasks", None),
                                task_id = g("task_id"),
                                text    = g("text", None),
                             ),
    }

    fn = routes.get(name)
    if fn:
        expanded = display_state.is_expanded()
        header_lines = _tool_header_lines(name, args, expanded)

        try:
            res = fn()
        except Exception as exc:
            elapsed = time.monotonic() - started
            for line in header_lines:
                print(f"{RED}{line}{RESET}")
            if expanded:
                print(f"{RED}[OUTPUT]{RESET}")
                print(f"{RED}{exc}{RESET}")
            print(f"{RED}[TOOL ERROR] ({elapsed:.2f}s){RESET}")
            return f"[TOOL ERROR] {name} raised an exception: {exc}"

        elapsed = time.monotonic() - started
        if res is None:
            normalized = "Success"
        elif isinstance(res, str):
            normalized = res
        elif isinstance(res, (dict, list)):
            normalized = json.dumps(res, ensure_ascii=False)
        else:
            normalized = str(res)

        for line in header_lines:
            print(f"{GRAY}{line}{RESET}")
        if expanded:
            print(f"{GRAY}[OUTPUT]{RESET}")
            print(f"{GRAY}{normalized}{RESET}")
        print(f"{GRAY}[TOOL DONE] ({elapsed:.2f}s){RESET}")

        return normalized
    print(f"{RED}[ERROR] Unknown tool: {name}{RESET}")
    return f"[ERROR] Unknown tool: {name}"


_EXHAUSTED_COOLDOWN = 30


def _safe_bool(value) -> bool:
    """Normalize booleans coming from model-generated JSON arguments."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"false", "0", "no", "off", "none", "null", ""}:
            return False
        if normalized in {"true", "1", "yes", "on"}:
            return True
    return bool(value)


DEBUG = False

_last_response_metadata: dict = {}


def _dbg(*args) -> None:
    if DEBUG:
        print(f"[DEBUG]", *args)


def get_last_response_metadata() -> dict:
    """Return native reasoning metadata from the most recent response."""
    return dict(_last_response_metadata)


def _display_reasoning(
    provider_id: str,
    model_name: str,
    reasoning: object = None,
    reasoning_details: object = None,
) -> None:
    """Display provider reasoning, compactly or fully according to Ctrl+O."""
    if not reasoning and not reasoning_details:
        return

    print(f"\n{GRAY}[Reasoning | {provider_id}/{model_name}]{RESET}")

    text = str(reasoning).strip() if reasoning else ""
    if not text and isinstance(reasoning_details, list):
        parts = []
        for item in reasoning_details:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
            elif item:
                parts.append(str(item))
        text = "\n".join(parts).strip()
    elif not text and reasoning_details:
        text = str(reasoning_details).strip()

    if display_state.is_expanded():
        print(text)
    else:
        words = len(text.split()) if text else 0
        print(
            f"{GRAY}[reasoning details collapsed: {words} words] "
            f"Press Ctrl+O to expand.{RESET}"
        )

    print(f"{GRAY}[End reasoning]{RESET}\n")



_AGENT_SYSTEM_PROMPT = """You are the direct coding and reasoning agent version for Termux-AI.

Solve the user's task, especially software engineering, debugging, code editing,
project investigation, and multi-step reasoning.

Use the available project tools actively. Inspect real files before making
assumptions, edit files when asked, execute tests/commands when appropriate,
and verify changes. Never claim a change was made unless a tool actually made it.
Preserve the existing architecture unless a change is necessary for correctness.
"""


def _ask_with_slots(
    prompt: str,
    history: list[dict] | None,
    voice: bool,
    system_content: str,
    model_slots: list[dict],
) -> str:
    global _last_response_metadata
    base_messages: list[dict] = [{"role": "system", "content": system_content}]
    if history:
        base_messages.extend(history)
    base_messages.append({"role": "user", "content": prompt})

    slot = 0
    last_slot = -1
    messages: list[dict] = []
    base_len = len(base_messages)
    # Generic API-error policy for this request:
    # same key + same error -> retry up to five times; then change key.
    # A success resets the counter. Keys exhausted by five identical errors
    # are tracked locally, so ordinary server errors never permanently
    # blacklist credentials.
    consecutive_error_identity: tuple[str, str, str] | None = None
    consecutive_error_signature: str | None = None
    consecutive_error_count = 0
    forced_retry_key: tuple[str, str, str] | None = None
    exhausted_keys: dict[tuple[str, str], set[str]] = {}

    # Track which model/provider the current in-memory history belongs to.
    # Native reasoning is preserved only while continuing the same model.
    history_model_identity: tuple[str, str] | None = None

    # Guard against an agent repeatedly issuing the exact same failing tool call.
    # The model gets an explicit recovery instruction after two identical failures,
    # and the third identical attempt ends the turn instead of burning the context.
    tool_failure_counts: dict[str, int] = {}
    reasoning_exhaustion_retries: dict[tuple[str, str, str], int] = {}

    # Invalid request recovery is scoped to provider+model, not individual keys.
    # A malformed request will fail the same way for every key in an account.
    invalid_request_retries: set[tuple[str, str]] = set()

    while True:
        _clear_expired_state()

        if slot >= len(model_slots):
            _last_response_metadata = {
                "state": "all_models_unavailable",
                "reasoning_complete": False,
            }
            return (
                "[ERROR] All configured models are currently unavailable. "
                "No usable key/model combination remains."
            )

        slot_cfg = model_slots[slot]
        pid = slot_cfg["provider_id"]
        model_name = slot_cfg["name"]
        max_tok = slot_cfg["max_tokens"]

        if (pid, model_name) in _bad_models:
            slot += 1
            continue

        if _account_restricted_until.get(pid, 0.0) > time.monotonic():
            slot += 1
            continue

        # During a consecutive identical error, retry the exact same key.
        if forced_retry_key is not None:
            retry_pid, retry_model, retry_key = forced_retry_key
            if retry_pid == pid and retry_model == model_name:
                api_key = retry_key
            else:
                forced_retry_key = None
                api_key = _next_key(
                    pid, model_name, exhausted_keys.get((pid, model_name), set())
                )
        else:
            api_key = _next_key(
                pid, model_name, exhausted_keys.get((pid, model_name), set())
            )

        if api_key is None:
            slot += 1
            continue

        if slot != last_slot:
            source = messages if messages else list(base_messages)
            current_model_identity = (pid, model_name)
            preserve_native_reasoning = (
                history_model_identity == current_model_identity
            )
            if not preserve_native_reasoning:
                for previous in reversed(source):
                    if previous.get("role") != "assistant":
                        continue
                    preserve_native_reasoning = (
                        previous.get("_reasoning_provider") == pid
                        and previous.get("_reasoning_model") == model_name
                    )
                    break
            messages = _sanitize_messages_for_provider(
                source,
                pid,
                model_name=model_name,
                preserve_native_reasoning=preserve_native_reasoning,
            )
            history_model_identity = current_model_identity
            last_slot = slot
            carried = max(0, len(messages) - len(base_messages))
            if carried > 0:
                print(
                    f"{YELLOW}[{pid}/{model_name}] Continuing with "
                    f"{carried} accumulated message(s) from previous model.{RESET}"
                )
        else:
            _dbg(f"Key retry on [{pid}/{model_name}]")

        client = _make_client(pid, api_key)

        try:
            while True:
                kwargs: dict = {
                    "model": model_name,
                    "messages": messages,
                    "tools": TOOLS_DESCRIPTION,
                    "tool_choice": "auto",
                }
                if max_tok is not None:
                    kwargs["max_tokens"] = max_tok

                # Native reasoning controls documented by the providers.
                # Agent/normal routing keeps provider-native reasoning separate
                # from assistant content so it can be displayed and reused safely.
                if pid == "groq":
                    effort = slot_cfg.get("reasoning_effort", "high")
                    kwargs["reasoning_effort"] = effort
                elif pid == "openrouter":
                    kwargs["extra_body"] = {
                        "reasoning": {"enabled": True}
                    }
                elif pid == "nvidia" and slot_cfg.get("enable_thinking"):
                    kwargs["extra_body"] = {
                        "chat_template_kwargs": {"enable_thinking": True}
                    }

                _dbg(
                    f"\u2192 API call [{pid}/{model_name}] | "
                    f"messages: {len(messages)} | max_tokens: {max_tok}"
                )

                raw = client.chat.completions.with_raw_response.create(**kwargs)
                response = raw.parse()
                raw_json = json.loads(raw.text)
                choice = response.choices[0]
                finish_reason = choice.finish_reason
                msg_dict = _msg_to_dict(choice.message)

                # Any successful model response resets the generic API-error streak.
                consecutive_error_identity = None
                consecutive_error_signature = None
                consecutive_error_count = 0
                forced_retry_key = None

                reasoning = msg_dict.get("reasoning") or msg_dict.get(
                    "reasoning_content"
                )
                reasoning_details = msg_dict.get("reasoning_details")
                if reasoning or reasoning_details:
                    msg_dict["_reasoning_provider"] = pid
                    msg_dict["_reasoning_model"] = model_name

                _last_response_metadata = {
                    "provider": pid,
                    "model": model_name,
                    "reasoning": reasoning,
                    "reasoning_details": reasoning_details,
                    "reasoning_provider": pid if (reasoning or reasoning_details) else None,
                    "finish_reason": finish_reason,
                }

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

                if pid == "openrouter" and not msg_dict.get("reasoning_details"):
                    raw_message = (
                        raw_json.get("choices", [{}])[0]
                        .get("message", {})
                    )
                    raw_details = raw_message.get("reasoning_details")
                    if raw_details:
                        msg_dict["reasoning_details"] = raw_details
                        reasoning_details = raw_details
                        _last_response_metadata["reasoning_details"] = raw_details

                # Display the final native reasoning payload after all SDK/raw
                # fallback extraction has completed.
                _display_reasoning(
                    pid,
                    model_name,
                    reasoning,
                    reasoning_details,
                )

                if msg_dict.get("tool_calls"):
                    if finish_reason == "length":
                        print(
                            f"{RED}[{pid}/{model_name}] Tool call truncated. "
                            f"Injecting error and retrying.{RESET}"
                        )
                        messages.append(msg_dict)
                        for tc in msg_dict["tool_calls"]:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": (
                                    "[ERROR] Tool call was cut off by the token limit. "
                                    "Please retry with smaller or fewer arguments."
                                ),
                            })
                        continue

                    messages.append(msg_dict)
                    for tc in msg_dict["tool_calls"]:
                        tool_name = tc["function"].get("name", "unknown")
                        raw_args = tc["function"].get("arguments", "{}")
                        tool_result = _dispatch_tool(tc, voice=voice)

                        # Stable signature: identical tool + arguments + error.
                        failure_markers = (
                            "[ERROR]", "[TOOL ERROR]", "[PERMISSION DENIED]",
                            "[TIMEOUT]"
                        )
                        is_failure = (
                            isinstance(tool_result, str)
                            and tool_result.startswith(failure_markers)
                        )
                        signature = (
                            f"{tool_name}:{raw_args}"
                            if is_failure else ""
                        )

                        if is_failure:
                            count = tool_failure_counts.get(signature, 0) + 1
                            tool_failure_counts[signature] = count

                            if count == 2:
                                tool_result += (
                                    "\n[AGENT RECOVERY] This exact tool call has "
                                    "failed twice. Do not repeat the same call. "
                                    "Use a different tool/command or explain the blocker."
                                )
                            elif count >= 3:
                                tool_result += (
                                    "\n[AGENT RECOVERY] Repeated identical tool "
                                    "failure detected. The current turn will stop "
                                    "after this result to prevent an infinite loop."
                                )

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": tool_result,
                        })

                        if is_failure and tool_failure_counts.get(signature, 0) >= 3:
                            _last_response_metadata["state"] = "repeated_tool_failure"
                            _cm.set_tool_context(messages[base_len:])
                            return (
                                f"[ERROR] Agent stopped after repeated identical "
                                f"failure from tool '{tool_name}'."
                            )
                    continue

                partial = msg_dict.get("content") or ""

                # Reasoning-budget exhaustion is different from an ordinary
                # truncated final answer. If the model produced reasoning but
                # no final content, blindly asking it to "continue" can create
                # an infinite reasoning-only loop.
                if (
                    finish_reason == "length"
                    and (reasoning or reasoning_details)
                    and not partial.strip()
                ):
                    exhaustion_key = (pid, model_name, api_key)
                    retry_count = reasoning_exhaustion_retries.get(exhaustion_key, 0)
                    _last_response_metadata["state"] = "reasoning_exhausted"
                    _last_response_metadata["reasoning_complete"] = False

                    if retry_count == 0:
                        reasoning_exhaustion_retries[exhaustion_key] = 1
                        retry_cfg = slot_cfg.copy()

                        # Groq supports a reasoning-effort control. Drop from
                        # "high" to "medium" once so a final answer has room.
                        if pid == "groq" and kwargs.get("reasoning_effort") == "high":
                            retry_cfg["reasoning_effort"] = "medium"
                            slot_cfg = retry_cfg
                            max_tok = slot_cfg["max_tokens"]
                            print(
                                f"{YELLOW}[{pid}/{model_name}] "
                                f"Reasoning budget exhausted before a final answer. "
                                f"Retrying once with reasoning_effort=medium.{RESET}"
                            )
                            continue

                        print(
                            f"{YELLOW}[{pid}/{model_name}] "
                            f"Reasoning budget exhausted before a final answer. "
                            f"Falling back instead of continuing the same reasoning loop.{RESET}"
                        )

                    # No second reasoning-only retry for this exact
                    # key/model. Move to another key/model combination.
                    slot += 1
                    last_slot = -1
                    messages = []
                    continue

                if finish_reason == "length":
                    anchor = partial[-80:].strip() if partial else ""
                    print(
                        f"{RED}[{pid}/{model_name}] Token limit hit. "
                        f"Continuing from: '...{anchor}'{RESET}"
                    )
                    messages.append({"role": "assistant", "content": partial})
                    messages.append({
                        "role": "user",
                        "content": (
                            "Continue exactly from where you left off. "
                            f"Do not repeat anything. Last words: '...{anchor}'"
                        ),
                    })
                    continue

                _last_response_metadata["state"] = "complete"
                _last_response_metadata["reasoning_complete"] = bool(
                    reasoning or reasoning_details
                )
                _cm.set_tool_context(messages[base_len:])
                return _stitch_assistant_turns(
                    messages[base_len:], partial
                ) or "[EMPTY RESPONSE]"

        except Exception as exc:
            status = _error_status(exc)
            message = str(exc)
            s_lower = message.lower()

            if _is_context_error(exc) and max_tok and max_tok > 512:
                slot_cfg["max_tokens"] = max_tok // 2
                print(
                    f"{YELLOW}[{pid}/{model_name}] Context/token limit hit. "
                    f"Reducing max_tokens to {slot_cfg['max_tokens']} and retrying.{RESET}"
                )
                continue

            # All API/runtime errors use the same consecutive-error rule.
            # No 429/500/400-specific branch skips a key or model early.
            current_identity = (pid, model_name, api_key)
            error_signature = (
                f"status={status!r}|{type(exc).__name__}|{message.strip()}"
            )

            if (
                consecutive_error_identity == current_identity
                and consecutive_error_signature == error_signature
            ):
                consecutive_error_count += 1
            else:
                consecutive_error_identity = current_identity
                consecutive_error_signature = error_signature
                consecutive_error_count = 1

            print(
                f"{RED}[{pid}/{model_name}] API error "
                f"(status {status or 'unknown'}), attempt "
                f"{consecutive_error_count}/5 on current key.{RESET}"
            )

            if consecutive_error_count < 5:
                forced_retry_key = current_identity
                time.sleep(1)
                continue

            exhausted_keys.setdefault((pid, model_name), set()).add(api_key)
            remaining = len(API_KEYS.get(pid, [])) - len(
                exhausted_keys.get((pid, model_name), set())
            )
            print(
                f"{RED}[{pid}/{model_name}] Same error reached 5 consecutive "
                f"times. Changing API key; {max(remaining, 0)} key(s) remain "
                f"for this model.{RESET}"
            )

            consecutive_error_identity = None
            consecutive_error_signature = None
            consecutive_error_count = 0
            forced_retry_key = None
            last_slot = -1

            # Stay on this model while another key is available. Once every
            # key has reached five identical failures, move to the next model.
            continue




def ask_ai(
    prompt: str,
    history: list[dict] | None = None,
    voice: bool = False,
) -> str:
    memory_block = build_memory_block(prompt)
    system_content = (
        (memory_block + "\n\n" + SYSTEM_PROMPT)
        if memory_block else SYSTEM_PROMPT
    )
    return _ask_with_slots(
        prompt, history, voice, system_content, MODEL_SLOTS
    )


def ask_agent(
    prompt: str,
    history: list[dict] | None = None,
    voice: bool = False,
) -> str:
    """Execute one turn of the persistent direct coding/reasoning agent."""
    memory_block = build_memory_block(prompt)
    system_content = SYSTEM_PROMPT+"\n---\n"+_AGENT_SYSTEM_PROMPT
    if memory_block:
        system_content = memory_block + "\n\n" + system_content
    return _ask_with_slots(
        prompt, history, voice, system_content, AGENT_MODEL_SLOTS
    )


def run_agent_step(voice: bool = False) -> str:
    """Execute a single step of the agent: Supervisor -> Worker -> Critic loop."""
    global _in_agent_mode
    _in_agent_mode = True
    try:
        state = state_manager.load_state()
        if not state or state.get("status") != "active":
            return "No active project found."

        subtasks = state.get("subtasks", [])
        goal = state.get("goal", "Unknown")

        task = None
        active_id = state.get("active_task_id")
        if active_id is not None:
            task = next((t for t in subtasks if t["id"] == active_id and t["status"] in ("pending", "active")), None)

        if task is None:
            cursor = state.get("cursor")
            if cursor is not None:
                task = next((t for t in subtasks if t["id"] == cursor and t["status"] in ("pending", "active")), None)

        if task is None:
            task = next((t for t in subtasks if t["status"] in ("pending", "active")), None)

        if task is None:
            return "No pending or active subtasks found."

        task_id = task["id"]
        desc    = task["description"]
        retry_count = task.get("retry_count", 0)

        def _worker_call() -> str:
            prompt = (
                f"AGENT WORKER MODE\nProject Goal: {goal}\n"
                f"Execute Subtask {task_id}: {desc}\n\n"
                "Complete the task using available tools. Be precise and thorough."
            )
            return ask_ai(prompt, voice=voice)

        def _critic_call(worker_reply: str) -> str:
            prompt = (
                f"AGENT CRITIC MODE\nTask: {desc}\nWorker Output:\n{worker_reply}\n\n"
                "Verify if the task was completed correctly. "
                "Reply with exactly 'VERIFIED' or 'FAILED: <reason>'."
            )
            return ask_ai(prompt, voice=voice)

        state_manager.update_subtask(task_id, status="active",
                                     notes=f"Execution attempt {retry_count + 1} started.")
        worker_reply = _worker_call()
        state_manager.update_subtask(task_id, worker_output=worker_reply)

        critic_reply = _critic_call(worker_reply)
        state_manager.update_subtask(task_id, critic_output=critic_reply,
                                     verification=critic_reply)

        if "VERIFIED" in critic_reply.upper():
            state_manager.update_subtask(task_id, status="completed",
                                         notes="Verified by LLM critic.")
            return f"Subtask {task_id} completed and verified."

        if retry_count >= 1:
            state_manager.update_subtask(task_id, status="failed",
                                         notes=f"Final failure after retry. {critic_reply}")
            return f"Subtask {task_id} failed after retry."

        print(f"{YELLOW}[Agent] Critic rejected task {task_id}. Running retry...{RESET}")
        state_manager.update_subtask(task_id, retry_count=1, status="active",
                                     notes=f"Retry 1 triggered. Previous: {critic_reply}")

        retry_worker_reply = _worker_call()
        state_manager.update_subtask(task_id, worker_output=retry_worker_reply)

        retry_critic_reply = _critic_call(retry_worker_reply)
        state_manager.update_subtask(task_id, critic_output=retry_critic_reply,
                                     verification=retry_critic_reply)

        if "VERIFIED" in retry_critic_reply.upper():
            state_manager.update_subtask(task_id, status="completed",
                                         notes="Verified by LLM critic on retry.")
            return f"Subtask {task_id} completed on retry."
        else:
            state_manager.update_subtask(task_id, status="failed",
                                         notes=f"Final failure after retry. {retry_critic_reply}")
            return f"Subtask {task_id} failed after retry."
    finally:
        _in_agent_mode = False
