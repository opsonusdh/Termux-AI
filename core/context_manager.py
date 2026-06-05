"""
core/context_manager.py  —  Two-layer chunk-based memory system for Termux-AI.

Architecture
============
Raw store  (logs/chunks.jsonl)
    Permanent, append-only.  Every completed turn is written as one or more
    JSONL entries and never mutated.  Three entry types:

    1. Normal chunk   {"id": 3, "ts": "...", "messages": [...]}
    2. Split parent   {"id": 3, "ts": "...", "subchunks": ["3.1","3.2"]}
    3. Subchunk       {"id": "3.1", "parent_id": 3, "ts": "...", "messages": [...]}

    Stable IDs — parent IDs are monotone integers, subchunk IDs are
    "<parent>.<n>" strings — neither ever changes after first assignment.

Summaries  (logs/chunk_summaries.json)
    Per-parent-ID progressive compression: short → micro → oneline.
    Written by a background daemon thread after every completed reply.
    Keyed by parent integer ID; subchunks are summarised at parent level.

Active context assembly  (build_history)
    For each historical turn:
      • latest RAW_RECENCY_WINDOW parents  → full raw messages (reconstructed
        from normal chunk or concatenated subchunks)
      • older parents  → single [system] summary message, age-selected
    The current turn's user message is NOT included here; ask_ai() receives
    it as the prompt= argument.

Integration points  (minimal — three changes across two files)
    llm_client.py ask_ai():
        _cm.set_tool_context(messages[base_len:])   # ONE LINE before return
    interface.py chat loop:
        _cm.open_chunk(user_input)                  # before ask_ai()
        chunk_history = _cm.build_history()         # inject into call_history
        # ... ask_ai() ...
        _cm.close_chunk(reply)                      # after reply
        _cm.maybe_summarize_async()                 # non-blocking post-reply
        # REMOVE history.append() lines — build_history() serves full history

Chunk lifecycle rules  (per spec)
    • A chunk always begins with a user message.
    • A chunk may contain tool calls + results between user and final reply.
    • A chunk never splits inside a user message or final assistant reply.
    • Splitting occurs only at safe tool-call boundaries (after a tool result,
      before the next assistant tool-call turn).
    • Chunk IDs are stable forever.
"""

import json
import os
import sys
import threading
from datetime import datetime

# ── Path bootstrap ─────────────────────────────────────────────────────────
# core/ must be first so 'import tools' resolves to core/tools.py, not tools/.
_CORE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_CORE)
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)
if _ROOT not in sys.path:
    sys.path.insert(1, _ROOT)

import paths

# ── Paths ──────────────────────────────────────────────────────────────────
CHUNKS_FILE    = paths.CHUNKS_FILE
SUMMARIES_FILE = paths.CHUNK_SUMMARIES_FILE
os.makedirs(paths.LOGS_DIR, exist_ok=True)

# ── Tunables ───────────────────────────────────────────────────────────────
SUMMARIZE_THRESHOLD  = 3      # min parent chunks before summarization starts
RAW_RECENCY_WINDOW   = 2      # newest N parent chunks kept raw in active context
MAX_CHUNK_CHARS      = 8_000  # total JSON chars before a chunk is split
TARGET_SUBCHUNK_CHARS = 4_000  # target size per subchunk
COMPRESSION_STAGES   = ["short", "micro", "oneline"]
STAGE_CHAR_LIMITS    = {"short": 800, "micro": 300, "oneline": 120}
SUMMARIZER_MODEL     = "gemini-2.5-flash-lite"

# ── In-memory state ────────────────────────────────────────────────────────
# _parent_ids     : ordered list of parent integer IDs (stable, monotone)
# _chunk_index    : id (int or str) → entry dict, for O(1) retrieval
# _summaries      : parent_id (int) → {stage: text}, progressive compression
# _next_id        : next parent ID to assign (recovered from max(parent_ids)+1)
# _current_chunk  : partially assembled chunk for the turn in progress
# _pending_context: tool messages captured from ask_ai() for the open chunk
_parent_ids:      list[int]              = []
_chunk_index:     dict                   = {}   # int | str → entry dict
_summaries:       dict[int, dict]        = {}
_next_id:         int                    = 1
_current_chunk:   dict | None            = None
_pending_context: list[dict]             = []
_lock             = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════
# Startup — rebuild in-memory state from disk
# ══════════════════════════════════════════════════════════════════════════

def _load() -> None:
    global _parent_ids, _chunk_index, _summaries, _next_id

    _parent_ids  = []
    _chunk_index = {}

    if os.path.exists(CHUNKS_FILE):
        with open(CHUNKS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                eid = entry.get("id")
                if eid is None:
                    continue
                _chunk_index[eid] = entry
                # Only integer IDs are parent chunks
                if isinstance(eid, int):
                    _parent_ids.append(eid)

    # Recover next available parent ID
    int_ids  = [eid for eid in _chunk_index if isinstance(eid, int)]
    _next_id = (max(int_ids) + 1) if int_ids else 1

    _summaries = {}
    if os.path.exists(SUMMARIES_FILE):
        try:
            with open(SUMMARIES_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            _summaries = {int(k): v for k, v in raw.items()}
        except Exception:
            _summaries = {}


_load()


# ══════════════════════════════════════════════════════════════════════════
# Tool context capture  (called by llm_client.ask_ai before returning)
# ══════════════════════════════════════════════════════════════════════════

def set_tool_context(messages: list[dict]) -> None:
    """
    Called by ask_ai() with messages[base_len:] immediately before it returns.

    Stores the tool-related messages accumulated during this turn so that
    close_chunk() can insert them into the raw record.  Content-only assistant
    turns and length-continuation user turns are excluded — we want only the
    genuine tool call / result exchanges.
    """
    global _pending_context
    _pending_context = [
        m for m in messages
        if (m.get("role") == "assistant" and m.get("tool_calls"))
        or  m.get("role") == "tool"
    ]


# ══════════════════════════════════════════════════════════════════════════
# Chunk lifecycle
# ══════════════════════════════════════════════════════════════════════════

def open_chunk(user_input: str) -> None:
    """
    Call BEFORE ask_ai().  Reserves the next stable parent ID and begins
    assembling a chunk with the user message.
    """
    global _current_chunk, _next_id
    with _lock:
        cid           = _next_id
        _next_id     += 1
        _current_chunk = {
            "id":       cid,
            "ts":       datetime.now().isoformat(),
            "messages": [{"role": "user", "content": user_input}],
        }


def close_chunk(assistant_reply: str) -> int:
    """
    Call AFTER ask_ai() returns.  Finalises the chunk, injects tool context
    captured by set_tool_context(), checks size, splits into subchunks if
    needed, and writes to disk.  Returns the stable parent chunk ID.
    """
    global _current_chunk, _pending_context

    with _lock:
        if _current_chunk is None:
            return -1
        cid          = _current_chunk["id"]
        ts           = _current_chunk["ts"]
        user_msgs    = list(_current_chunk["messages"])
        tool_msgs    = list(_pending_context)
        _current_chunk   = None
        _pending_context = []

    # Assemble the full message sequence for this turn:
    #   user message → tool exchanges → final assistant reply
    msgs = user_msgs + tool_msgs + [{"role": "assistant", "content": assistant_reply}]

    # Size check (in serialised JSON chars, as a proxy for token weight)
    total_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs)

    if total_chars > MAX_CHUNK_CHARS:
        _write_split_chunk(cid, ts, msgs)
    else:
        _write_normal_chunk(cid, ts, msgs)

    return cid


def _write_normal_chunk(cid: int, ts: str, msgs: list[dict]) -> None:
    """Write a single normal chunk (unsplit)."""
    entry = {"id": cid, "ts": ts, "messages": msgs}
    with open(CHUNKS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    with _lock:
        _chunk_index[cid] = entry
        _parent_ids.append(cid)


def _write_split_chunk(parent_id: int, ts: str, msgs: list[dict]) -> None:
    """
    Split a large chunk into subchunks at safe tool-call boundaries and write
    all entries to disk in one pass.

    Split rule: a safe split point is after a 'tool' role message, when the
    next message is an assistant message with tool_calls (start of a new
    exchange round).  We accumulate until TARGET_SUBCHUNK_CHARS, then split
    at the next safe boundary.

    If no safe split point exists (e.g. one enormous tool result), the chunk
    is stored unsplit — we never break in the middle of a call/result pair.
    """
    segments: list[list[dict]] = []
    current:  list[dict]       = []
    current_size               = 0

    for i, msg in enumerate(msgs):
        msg_size = len(json.dumps(msg, ensure_ascii=False))
        current.append(msg)
        current_size += msg_size

        is_tool_result  = msg.get("role") == "tool"
        is_last         = (i == len(msgs) - 1)
        next_is_tc_call = (
            not is_last
            and isinstance(msgs[i + 1].get("tool_calls"), list)
            and msgs[i + 1].get("role") == "assistant"
        )

        # Only split when we have enough material AND we're at a safe boundary.
        # Never split if the next message is the final assistant reply or we
        # just started the current segment (avoids empty first segments).
        if (
            is_tool_result
            and not is_last
            and next_is_tc_call
            and current_size >= TARGET_SUBCHUNK_CHARS
            and len(current) > 1
        ):
            segments.append(current)
            current      = []
            current_size = 0

    if current:
        segments.append(current)

    # If we couldn't find any split point, store unsplit
    if len(segments) == 1:
        _write_normal_chunk(parent_id, ts, msgs)
        return

    subchunk_ids = [f"{parent_id}.{n + 1}" for n in range(len(segments))]
    parent_entry = {"id": parent_id, "ts": ts, "subchunks": subchunk_ids}
    sc_entries   = [
        {"id": sc_id, "parent_id": parent_id, "ts": ts, "messages": seg}
        for sc_id, seg in zip(subchunk_ids, segments)
    ]

    with open(CHUNKS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(parent_entry, ensure_ascii=False) + "\n")
        for sc in sc_entries:
            f.write(json.dumps(sc, ensure_ascii=False) + "\n")

    with _lock:
        _chunk_index[parent_id] = parent_entry
        for sc in sc_entries:
            _chunk_index[sc["id"]] = sc
        _parent_ids.append(parent_id)


# ══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════════════

def _get_messages_for_parent(parent_id: int) -> list[dict]:
    """
    Return the full ordered message list for a parent, whether stored directly
    or spread across subchunks.
    """
    entry = _chunk_index.get(parent_id, {})
    if "messages" in entry:
        return list(entry["messages"])
    if "subchunks" in entry:
        out = []
        for sc_id in entry["subchunks"]:
            sc = _chunk_index.get(sc_id, {})
            out.extend(sc.get("messages", []))
        return out
    return []


def _chunk_to_text(parent_id: int) -> str:
    """Render a parent's full interaction as plain text for the summariser."""
    parts = []
    for msg in _get_messages_for_parent(parent_id):
        role    = msg.get("role", "")
        content = msg.get("content") or ""
        if role == "user":
            parts.append(f"User: {content}")
        elif role == "assistant" and not msg.get("tool_calls"):
            parts.append(f"Assistant: {content}")
        elif role == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                name = tc.get("function", {}).get("name", "tool")
                args = tc.get("function", {}).get("arguments", "")
                parts.append(f"Tool call ({name}): {str(args)[:300]}")
        elif role == "tool":
            res = content if isinstance(content, str) else str(content)
            parts.append(f"Tool result: {res[:400]}")
    return "\n".join(parts)


def _pick_summary(parent_id: int, age: int, summaries: dict) -> str:
    """
    Select the best available summary level for age.
    age 0 → most recently summarised → prefer short (most detail).
    age 1 → prefer micro.
    age ≥ 2 → prefer oneline (minimum size).
    Falls back through remaining stages if preferred is absent.
    """
    s = summaries.get(parent_id, {})
    if not s:
        return ""
    if age == 0:
        order = ["short", "micro", "oneline"]
    elif age == 1:
        order = ["micro", "short", "oneline"]
    else:
        order = ["oneline", "micro", "short"]
    for stage in order:
        if stage in s:
            return s[stage]
    return ""


def _msg_to_history(msg: dict) -> list[dict]:
    """
    Convert a stored chunk message dict to one or more history message dicts
    suitable for the OpenAI API.  Returns a list (usually length 1, but tool
    result messages fan out correctly).
    """
    role    = msg.get("role", "")
    content = msg.get("content")

    if role == "user" and content:
        return [{"role": "user", "content": content}]

    if role == "assistant":
        if msg.get("tool_calls"):
            return [{"role": "assistant", "content": None,
                     "tool_calls": msg["tool_calls"]}]
        if content:
            return [{"role": "assistant", "content": content}]
        return []

    if role == "tool":
        # tool_call_id lives at the message level, not inside content
        tc_id   = msg.get("tool_call_id", "")
        tc_body = content if isinstance(content, str) else json.dumps(content)
        return [{"role": "tool", "tool_call_id": tc_id, "content": tc_body}]

    return []


# ══════════════════════════════════════════════════════════════════════════
# Active context assembly
# ══════════════════════════════════════════════════════════════════════════

def build_history() -> list[dict]:
    """
    Assemble the history list to pass to ask_ai(history=...).

    Layout (oldest → newest):
      [system] "Chunk 1: <oneline summary>"
      [system] "Chunk 2: <micro summary>"
      [system] "Chunk 3: <short summary>"
      [user]   raw message from chunk N-1
      [assistant] ...
      [tool call / result] ...
      [user]   raw message from chunk N
      [assistant] ...

    The current turn's user message is NOT here — ask_ai() gets it as prompt=.
    """
    with _lock:
        parent_ids = list(_parent_ids)
        summaries  = dict(_summaries)
        index      = dict(_chunk_index)

    if not parent_ids:
        return []

    history: list[dict] = []
    cutoff = max(0, len(parent_ids) - RAW_RECENCY_WINDOW)

    for pos, pid in enumerate(parent_ids):
        if pos < cutoff:
            # ── Summarised range ─────────────────────────────────────────
            age          = cutoff - 1 - pos   # 0 = newest summarised
            summary_text = _pick_summary(pid, age, summaries)

            if summary_text:
                history.append({
                    "role":    "system",
                    "content": f"[Chunk {pid}] {summary_text}",
                })
            else:
                # No summary generated yet — emit a minimal snippet
                entry = index.get(pid, {})
                if "subchunks" in entry:
                    first_sc = index.get(entry["subchunks"][0], {})
                    first_user = next(
                        (m["content"] for m in first_sc.get("messages", [])
                         if m.get("role") == "user"), ""
                    )
                else:
                    first_user = next(
                        (m["content"] for m in entry.get("messages", [])
                         if m.get("role") == "user"), ""
                    )
                snippet = (first_user[:200] + "…") if len(first_user) > 200 else first_user
                history.append({
                    "role":    "system",
                    "content": f"[Chunk {pid} — pending summary] {snippet}",
                })

        else:
            # ── Raw range — inject full conversation turns ───────────────
            msgs = _get_messages_for_parent(pid)
            for msg in msgs:
                history.extend(_msg_to_history(msg))

    return history


# ══════════════════════════════════════════════════════════════════════════
# Retrieval tools  (callable from llm_client._dispatch_tool)
# ══════════════════════════════════════════════════════════════════════════

def retrieve_chunk(chunk_id) -> str:
    """
    Return the full raw chunk or subchunk by its stable ID.

    chunk_id may be:
      • int  — a parent chunk (returns messages or subchunk reference list)
      • str  — either a stringified int "3" or a subchunk ID "3.1"

    If the requested parent has subchunks, returns the parent index entry
    with the list of subchunk IDs.  The caller can then retrieve individual
    subchunks by their string ID.
    """
    with _lock:
        index = dict(_chunk_index)

    # Normalise: try direct lookup, then int conversion, then str
    entry = (
        index.get(chunk_id)
        or index.get(int(chunk_id) if str(chunk_id).isdigit() else chunk_id)
        or index.get(str(chunk_id))
    )
    if entry is None:
        return json.dumps({"error": f"Chunk '{chunk_id}' not found."})
    return json.dumps(entry, ensure_ascii=False, indent=2)


def list_chunks() -> str:
    """
    Return a JSON list of all parent chunk IDs with their one-line summaries
    (or a first-message snippet if not yet summarised).  Includes a 'split'
    flag and subchunk count for parents that were split.
    """
    with _lock:
        parent_ids = list(_parent_ids)
        summaries  = dict(_summaries)
        index      = dict(_chunk_index)

    result = []
    for pid in parent_ids:
        entry     = index.get(pid, {})
        is_split  = "subchunks" in entry
        oneline   = summaries.get(pid, {}).get("oneline", "")

        if not oneline:
            if is_split:
                first_sc = index.get(entry["subchunks"][0], {})
                first_user = next(
                    (m["content"] for m in first_sc.get("messages", [])
                     if m.get("role") == "user"), ""
                )
            else:
                first_user = next(
                    (m["content"] for m in entry.get("messages", [])
                     if m.get("role") == "user"), ""
                )
            oneline = first_user[:80]

        rec = {"id": pid, "ts": entry.get("ts", ""), "oneline": oneline}
        if is_split:
            rec["subchunks"] = entry["subchunks"]
        result.append(rec)

    return json.dumps(result, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════════════════
# Summarisation  —  background, strictly post-reply
# ══════════════════════════════════════════════════════════════════════════

def maybe_summarize_async() -> None:
    """
    Spawn a background daemon thread to summarise old chunks.
    Must be called AFTER close_chunk() — never during inference or between
    tool calls.
    """
    with _lock:
        n = len(_parent_ids)
    if n <= SUMMARIZE_THRESHOLD:
        return
    t = threading.Thread(target=_summarize_old_chunks, daemon=True)
    t.start()


def _summarize_old_chunks() -> None:
    """
    Background worker.  Generates the next missing compression stage for
    up to 3 eligible parent chunks per post-reply invocation.
    """
    with _lock:
        parent_ids = list(_parent_ids)
        summaries  = dict(_summaries)

    # Only chunks outside the raw recency window are eligible
    eligible = parent_ids[:max(0, len(parent_ids) - RAW_RECENCY_WINDOW)]
    if not eligible:
        return

    processed = 0
    for pid in eligible:
        existing   = summaries.get(pid, {})
        next_stage = next(
            (s for s in COMPRESSION_STAGES if s not in existing), None
        )
        if next_stage is None:
            continue   # fully compressed

        text = _call_summarizer(pid, next_stage, existing)
        if text:
            with _lock:
                _summaries.setdefault(pid, {})[next_stage] = text
            _persist_summaries()
            processed += 1

        if processed >= 3:
            break


def _call_summarizer(parent_id: int, stage: str, existing: dict) -> str:
    """One lightweight LLM call to produce a single summary stage."""
    char_limit = STAGE_CHAR_LIMITS[stage]

    if stage == "short":
        source = _chunk_to_text(parent_id)
        instr  = (
            f"Summarize this conversation chunk in under {char_limit} characters. "
            "Preserve all important facts, code, tool results, decisions, and outcomes. "
            "Drop pleasantries. Be precise and dense."
        )
    elif stage == "micro":
        source = existing.get("short") or _chunk_to_text(parent_id)
        instr  = (
            f"Compress to under {char_limit} characters. "
            "Keep only the most essential facts and outcomes."
        )
    elif stage == "oneline":
        source = (
            existing.get("micro")
            or existing.get("short")
            or _chunk_to_text(parent_id)
        )
        instr = (
            f"Reduce to a single line under {char_limit} characters. "
            "Capture only the key outcome."
        )
    else:
        source = _chunk_to_text(parent_id)
        instr  = f"Summarize in under {char_limit} characters."

    prompt     = f"{instr}\n\n---\n{source}"
    sys_prompt = (
        "You are a concise summarizer. Output only the summary text. "
        "No preamble, no labels, no markdown."
    )
    try:
        from tools import ask_ai_simple
        return ask_ai_simple(prompt, SUMMARIZER_MODEL, sys_prompt)
    except Exception:
        return ""


def _persist_summaries() -> None:
    with _lock:
        data = {str(k): v for k, v in _summaries.items()}
    try:
        with open(SUMMARIES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
# Legacy shims — safe to call, no-ops
# ══════════════════════════════════════════════════════════════════════════

def log_turn(role: str, content: str) -> None:          pass
def trigger_compression() -> None:                      pass
def update_summary(summary_text: str) -> None:          pass

def get_summary() -> str:
    """Legacy shim — returns flat one-line summaries for monitoring."""
    with _lock:
        parent_ids = list(_parent_ids)
        summaries  = dict(_summaries)
    lines = []
    for pid in parent_ids:
        oneline = summaries.get(pid, {}).get("oneline", "")
        if oneline:
            lines.append(f"[{pid}] {oneline}")
    return "\n".join(lines)
