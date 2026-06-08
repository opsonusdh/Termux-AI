import os
import sys
import time
import json

#  Path bootstrap 
_CORE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_CORE)
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)
if _ROOT not in sys.path:
    sys.path.insert(1, _ROOT)

import paths
from agent import state_manager
from openai import OpenAI
from renderer import RED, YELLOW, RESET
from tools import *
import context_manager as _cm

with open(paths.PROMPT_FILE) as file:
    SYSTEM_PROMPT = file.read()

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
    {"provider_id":"google",  "name": "gemini-3-flash-preview",                "max_tokens": None},
    {"provider_id": "groq",  "name": "openai/gpt-oss-120b",                  "max_tokens": 4096},
    {"provider_id": "nvidia",  "name": "deepseek-ai/deepseek-v4-flash",        "max_tokens": 4096},
    {"provider_id": "nvidia",  "name": "deepseek-ai/deepseek-r1",              "max_tokens": 4096},
    {"provider_id": "google",  "name": "gemma-4-31b-it",                       "max_tokens": None},
    {"provider_id": "google",  "name": "gemini-2.5-flash",                     "max_tokens": None},
    {"provider_id": "google",  "name": "gemini-3.1-flash-lite",                "max_tokens": None},
    {"provider_id": "groq",    "name": "qwen/qwen3-32b",                       "max_tokens": None},
    {"provider_id": "groq",    "name": "deepseek-r1-distill-llama-70b",        "max_tokens": None},
    {"provider_id": "nvidia",  "name": "nvidia/llama-3.1-nemotron-nano-8b-v1", "max_tokens": 4096},
    {"provider_id": "google",  "name": "gemini-2.5-flash-lite",                "max_tokens": None},
]


def _load_api_keys() -> dict[str, list[str]]:
    path = paths.API_KEYS_FILE
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
    if isinstance(msg, dict):
        return msg

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


_DUMMY_SIG = "context_engineering_is_the_way_to_go"

_GEMINI_PROVIDERS = {"google"}


def _sanitize_messages_for_provider(messages: list[dict], pid: str) -> list[dict]:
    going_to_gemini = pid in _GEMINI_PROVIDERS
    result = []

    for m in messages:
        if not isinstance(m, dict):
            result.append(m)
            continue

        # All providers require content to be a string, never a raw dict/list.
        # This guards against diagnosis dicts or other structured data being
        # injected directly as message content (e.g. from _diag_future.result()).
        content = m.get("content")
        if isinstance(content, (dict, list)):
            m = {**m, "content": json.dumps(content, ensure_ascii=False)}

        if "tool_calls" not in m:
            result.append(m)
            continue

        new_tcs = []
        for i, tc in enumerate(m["tool_calls"]):
            tc = dict(tc)
            has_sig = (
                isinstance(tc.get("extra_content"), dict)
                and tc["extra_content"].get("google", {}).get("thought_signature")
            )

            if going_to_gemini and not has_sig:
                if i == 0:
                    tc["extra_content"] = {"google": {"thought_signature": _DUMMY_SIG}}

            elif not going_to_gemini and has_sig:
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
        "send_whatsapp_message": lambda: send_whatsapp_message(
                                to_phone     = g("to_phone", ""),
                                message_text = g("message_text", ""),
                            ),
          "get_whatsapp_status": lambda: get_whatsapp_status(),
          "get_whatsapp_chats": lambda: get_whatsapp_chats(
                                  filter_type  = g("filter_type", "all")
                              ),
          "get_pending_whatsapp_messages": lambda: get_pending_whatsapp_messages(
                                clear        = bool(g("clear", True)),
                            ),
        "fetch_whatsapp_chat_history": lambda: fetch_whatsapp_chat_history(
                                to_phone     = g("to_phone", ""),
                                limit        = int(g("limit", 5)),
                            ),
        "set_whatsapp_busy_mode": lambda: set_whatsapp_busy_mode(
                                enabled      = bool(g("enabled", False)),
                                instruction  = g("instruction", ""),
                                exclude_all_groups_except = g("exclude_all_groups_except", None)
                            ),
        "get_whatsapp_report": lambda: get_whatsapp_report(
                                clear        = bool(g("clear", False)),
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
                                archive = bool(g("archive", True)),
                            ),
        "set_whatsapp_seen": lambda: set_whatsapp_seen(
                                jid = g("jid", ""),
                            ),
    }

    fn = routes.get(name)
    if fn:
        res = fn()
        if res is None:
            return "Success"
        if isinstance(res, str):
            return res
        if isinstance(res, (dict, list)):
            return json.dumps(res, ensure_ascii=False)
        return str(res)
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

    # NOTE: No state injection here. Agent context is only active during
    # run_agent_step() calls triggered by /agent. Normal chat is unaffected.

    base_messages: list[dict] = [{"role": "system", "content": system_content}]
    if history:
        base_messages.extend(history)
    base_messages.append({"role": "user", "content": prompt})

    slot      = 0
    last_slot = -1
    messages  = []
    base_len  = len(base_messages)

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

        
        if slot != last_slot:
            source    = messages if messages else list(base_messages)
            messages  = _sanitize_messages_for_provider(source, pid)
            last_slot = slot
            carried   = len(messages) - len(base_messages)
            if carried > 0:
                print(f"{YELLOW}[{pid}/{model_name}] Continuing with {carried} accumulated message(s) from previous model.{RESET}")
            _dbg(f"Slot changed → [{pid}/{model_name}]. "
                 f"Messages carried: {len(messages)} (base: {len(base_messages)})")
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

                raw           = client.chat.completions.with_raw_response.create(**kwargs)
                response      = raw.parse()
                raw_json      = json.loads(raw.text)

                choice        = response.choices[0]
                finish_reason = choice.finish_reason
                msg_dict      = _msg_to_dict(choice.message)

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

                # Capture tool context into the open chunk BEFORE returning.
                # messages[base_len:] contains every intermediate assistant
                # tool-call turn and tool-result turn accumulated this call.
                _cm.set_tool_context(messages[base_len:])
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

            elif any(x in s.lower() for x in ("context", "token_limit", "max_tokens", "exceed", "excluding", "window")):
                if max_tok and max_tok > 512:
                    new_max = max_tok // 2
                    print(
                        f"{YELLOW}[{pid}/{model_name}] Context/token limit hit. "
                        f"Reducing max_tokens from {max_tok} to {new_max} and retrying...{RESET}"
                    )
                    slot_cfg["max_tokens"] = new_max
                    continue
                else:
                    print(
                        f"{RED}[{pid}/{model_name}] Context window exceeded or max_tokens already minimal. "
                        f"Advancing to next slot...{RESET}"
                    )
                    slot += 1
                    continue

            elif "API_KEY_INVALID" in s:
                print(f"{RED}[{pid}] Invalid API key — skipping '{model_name}'.{RESET}")
                slot += 1

            else:
                _dbg(f"  Unhandled exception: {s[:300]}")
                raise

def run_agent_step(voice: bool = False) -> str:
    """Execute a single step of the agent: Supervisor -> Worker -> Critic loop.

    Recovery priority:
      1. active_task_id (interrupted mid-execution)
      2. cursor (last known position)
      3. first pending task (fallback for fresh start or corrupt cursor)

    One retry maximum: the retry executes immediately in the same call.
    worker_output and critic_output are persisted after every LLM call.
    No subprocess. No shell execution.
    """
    state = state_manager.load_state()
    if not state or state.get("status") != "active":
        return "No active project found."

    subtasks = state.get("subtasks", [])
    goal = state.get("goal", "Unknown")

    # --- Supervisor: resolve which task to run (priority chain) ---
    task = None

    # 1. active_task_id takes priority — we were interrupted mid-execution
    active_id = state.get("active_task_id")
    if active_id is not None:
        task = next((t for t in subtasks if t["id"] == active_id and t["status"] in ("pending", "active")), None)

    # 2. cursor position
    if task is None:
        cursor = state.get("cursor")
        if cursor is not None:
            task = next((t for t in subtasks if t["id"] == cursor and t["status"] in ("pending", "active")), None)

    # 3. first pending task (recovery fallback)
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

    # --- Worker Phase (attempt 1) ---
    state_manager.update_subtask(task_id, status="active",
                                 notes=f"Execution attempt {retry_count + 1} started.")
    worker_reply = _worker_call()
    state_manager.update_subtask(task_id, worker_output=worker_reply)

    # --- Critic Phase (attempt 1) ---
    critic_reply = _critic_call(worker_reply)
    state_manager.update_subtask(task_id, critic_output=critic_reply,
                                 verification=critic_reply)

    if "VERIFIED" in critic_reply.upper():
        state_manager.update_subtask(task_id, status="completed",
                                     notes="Verified by LLM critic.")
        return f"Subtask {task_id} completed and verified."

    # Critic says FAILED. One retry allowed.
    if retry_count >= 1:
        # Already used the one retry — mark final failure.
        state_manager.update_subtask(task_id, status="failed",
                                     notes=f"Final failure after retry. {critic_reply}")
        return f"Subtask {task_id} failed after retry."

    # --- Single retry: actually re-execute, don't just mark pending ---
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
