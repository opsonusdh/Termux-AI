"""
core/context_compressor.py — Compression of the active context.

Why this module exists
======================
When an agent's context and thinking budget outgrow the model's context
window, the old recovery path was: shrink ``max_tokens`` and move on to the
next model.  That is a reasonable answer for a model that has just started
working — a fresh model that cannot even hold the prompt is probably the
wrong model.  It is the wrong answer for work that runs for days: a
long-running task piles up hundreds of kilobytes of reasoning traces, tool
calls and tool outputs, and handing that to a different model (or dropping
it) loses the thread of the work.

The budget comes from the *provider*, not from a local registry of context
windows.  A registry is a guess about an endpoint that may enforce a
different limit than the model card claims — and when the guess is wrong,
either work that did not need compressing gets compressed, or the request
is sent as it is and the provider answers with the real limit in the error
message.  So the request goes out unchanged, and the compression is driven
by the provider's own answer: the window it states is the budget the
history is compressed into.

The approach here is:

  1. **Compress** the *oldest* passes of the active context — the reasoning
     details, the tool-call arguments and the raw tool outputs — into a
     short, dense digest.
  2. **Archive** the discarded raw text on disk under a stable id.
  3. **Point at the archive** from a marker left in the conversation, so the
     model can pull the raw content back with ``retrieve_archive(id)``,
     check how much work was already done, and continue from there.

Compression is progressive and *partial*.  It starts with the most distant
passes and stops as soon as the payload fits the budget, so the recent work
stays raw.  It never rewrites the whole window.

Three levels are used, from mild to drastic:

  level 1  digest   — reasoning dropped, tool arguments and tool outputs
                      truncated to a readable head, prose kept.
  level 2  compact  — tool arguments replaced by a size marker, tool outputs
                      cut to a short head, prose cut harder.
  level 3  stub     — the whole pass collapses to one line naming the tools
                      it used and the archive id holding the raw text.

Safety rules enforced here
==========================
* The leading system messages (the system prompt and the chunk summaries) are
  never compressed — they are already the compressed form of older work.
* The newest ``KEEP_RECENT_MESSAGES`` messages are kept raw, so the current
  turn reaches the model intact.  That preference yields when the protected
  part alone does not fit the budget: there is no other way under the limit,
  and the raw text is archived either way.
* The prompt being answered is never reduced to a stub.
* Tool calls keep their identity.  A call's *arguments* are shortened, but the
  call, its id and its name survive — and the arguments stay parseable JSON,
  because the provider parses them.
* A tool call and its result are never separated.  A pass whose result lies
  outside the region keeps the call in place (only its arguments shrink), and
  a result whose call lies outside the region is truncated rather than
  stubbed away.  Either way the pairing survives.
* If compression would ever break a tool-call / tool-result pairing — or
  leave a call without its answer — the original message list is returned
  unchanged.

The archive is append-only (``logs/context_archive.jsonl``), so a compressed
segment survives process restarts and can be read back days later.
"""

import hashlib
import json
import os
import re
import sys
import threading
from datetime import datetime

# ── Path bootstrap ─────────────────────────────────────────────────────────
# core/ must be first so 'import paths' resolves to the project root copy.
_CORE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_CORE)
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)
if _ROOT not in sys.path:
    sys.path.insert(1, _ROOT)

import paths

ARCHIVE_FILE = paths.CONTEXT_ARCHIVE_FILE


# ── Singleton registration ─────────────────────────────────────────────────
# This module is imported under two names in this codebase: "core.context_compressor"
# and the bare "context_compressor" — llm_client.py puts core/ on sys.path and
# imports its siblings by their bare names.  Python treats those as two
# different modules, so this file would be executed twice and each copy would
# keep its own archive index.  A segment archived through one copy would then
# be invisible to the other, and the model would be told "Archive 'A7' not
# found" for a segment that demonstrably exists on disk.
#
# Registering the module under both names guarantees a single instance and a
# single index, whatever order the imports happen in.
sys.modules.setdefault("context_compressor", sys.modules[__name__])
sys.modules.setdefault("core.context_compressor", sys.modules[__name__])
try:
    # Also bind it as an attribute of the package, so that
    # "from core import context_compressor" resolves to this same object.
    import importlib as _importlib
    _pkg = _importlib.import_module("core")
    if getattr(_pkg, "context_compressor", None) is not sys.modules[__name__]:
        _pkg.context_compressor = sys.modules[__name__]
except Exception:
    pass

# ── Tunables ───────────────────────────────────────────────────────────────
CHARS_PER_TOKEN        = 4.0     # rough English/code ratio for the estimate
MESSAGE_OVERHEAD       = 4       # tokens of role/format scaffolding per message
IMAGE_TOKEN_COST       = 1500    # a picture costs roughly this many tokens
KEEP_RECENT_MESSAGES   = 8       # newest messages that are never compressed
# The live turn keeps less: only its last two messages stay raw, so one
# enormous tool output — or one enormous tool-call argument — can still be
# compressed.  The prompt being answered lives at the end of the *head*, and
# is protected there.
TAIL_KEEP_RECENT        = 2
TAIL_PROTECT_FIRST      = 0

# level 1 — digest
L1_CALL_CHARS          = 240
L1_OUTPUT_CHARS        = 400
L1_PROSE_CHARS         = 2000
L1_USER_CHARS          = 4000
# level 2 — compact
L2_OUTPUT_CHARS        = 120
L2_PROSE_CHARS         = 600
L2_USER_CHARS          = 1500

MAX_RETRIEVE_CHARS     = 200_000
DEFAULT_RETRIEVE_CHARS = 20_000

# ── In-memory state ────────────────────────────────────────────────────────
# _index    : archive_id → metadata (+ byte offset of the JSONL line)
# _next_seq : monotone counter behind the "A<n>" ids
_lock  = threading.RLock()
_index: dict[str, dict] = {}
_next_seq: int = 1
# _index_sig : (mtime_ns, size) of the archive file when _index was built.
#              The index is a cache; the file is the truth.
_index_sig: tuple = ()


def _file_sig() -> tuple:
    """A cheap fingerprint of the archive file — it changes when appended to."""
    try:
        st = os.stat(ARCHIVE_FILE)
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def _sync_index() -> None:
    """
    Refresh the index when the archive file changed underneath us.

    Another process (or another instance of this module) may have appended to
    the archive.  A lookup must not conclude "not found" from a stale cache
    alone, so the file is re-read whenever its fingerprint moved.
    """
    global _index_sig
    sig = _file_sig()
    if sig != _index_sig:
        _load()
        _index_sig = _file_sig()


def _load() -> None:
    """Rebuild the archive index from disk."""
    global _next_seq, _index_sig
    with _lock:
        _index.clear()
        _next_seq = 1
        if not os.path.exists(ARCHIVE_FILE):
            _index_sig = _file_sig()
            return
        try:
            with open(ARCHIVE_FILE, "rb") as f:
                offset = 0
                for raw in f:
                    start = offset
                    offset += len(raw)
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    aid = entry.get("id")
                    if not aid:
                        continue
                    _index[str(aid)] = {
                        "id": str(aid),
                        "ts": entry.get("ts", ""),
                        "label": entry.get("label", ""),
                        "level": entry.get("level", 0),
                        "chars": entry.get("chars", 0),
                        "messages": len(entry.get("messages", []) or []),
                        "seq": _seq_of(aid),
                        "offset": start,
                        "hash": entry.get("hash", ""),
                    }
                    _next_seq = max(_next_seq, _seq_of(aid) + 1)
        except OSError:
            pass
        _index_sig = _file_sig()


def _seq_of(archive_id) -> int:
    """Extract the numeric part of an "A<n>" id (0 when it has none)."""
    m = re.match(r"^A(\d+)$", str(archive_id))
    return int(m.group(1)) if m else 0


_load()


# ══════════════════════════════════════════════════════════════════════════
# Size estimation
# ══════════════════════════════════════════════════════════════════════════

def _message_chars(msg: dict) -> int:
    """Character weight of one message, counting only the payload text."""
    if not isinstance(msg, dict):
        return 0
    total = 0
    content = msg.get("content")
    if isinstance(content, str):
        total += len(content)
    elif content is not None:
        try:
            total += len(json.dumps(content, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            total += len(str(content))
    for tc in msg.get("tool_calls") or []:
        try:
            total += len(json.dumps(tc, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            total += len(str(tc))
    for key in ("reasoning", "reasoning_content", "reasoning_details"):
        val = msg.get(key)
        if val:
            try:
                total += len(json.dumps(val, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                total += len(str(val))
    return total


def _chars_of(messages: list[dict]) -> int:
    return sum(_message_chars(m) for m in messages)


def estimate_tokens(messages: list[dict]) -> int:
    """
    Estimate the token weight of a message list.

    This is a character-based approximation, not a tokenizer.  It only has to
    be good enough to decide *when* to compress; a real "context length
    exceeded" error from the provider is the authoritative signal and is
    handled by the reactive path in llm_client.
    """
    if not messages:
        return 0
    total = 0
    for msg in messages:
        total += int(_message_chars(msg) / CHARS_PER_TOKEN) + MESSAGE_OVERHEAD
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in (
                    "orion_image", "image_url", "image",
                ):
                    total += IMAGE_TOKEN_COST
    return total


# ══════════════════════════════════════════════════════════════════════════
# Pass grouping
# ══════════════════════════════════════════════════════════════════════════

def _group_passes(messages: list[dict]) -> list[dict]:
    """
    Group messages into *passes*.

    A pass is either an assistant turn that issued tool calls together with
    the tool results that answered it, or a standalone message.  Passes are
    the unit of compression: collapsing a whole pass keeps the tool-call /
    tool-result pairing intact, which a per-message rewrite would not.
    """
    groups: list[dict] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("tool_calls"):
            j = i + 1
            while j < n and isinstance(messages[j], dict) \
                    and messages[j].get("role") == "tool":
                j += 1
            groups.append({
                "kind": "tools",
                "msgs": messages[i:j],
                "start": i,
                "end": j,
            })
            i = j
        else:
            groups.append({
                "kind": "single",
                "msgs": [msg],
                "start": i,
                "end": i + 1,
            })
            i += 1
    return groups


def _group_is_complete(group: dict) -> bool:
    """
    True when every assistant tool call in the pass has its tool result.

    An incomplete pass is never compressed: dropping half of it would leave
    a tool message without its originating tool call, which every provider
    rejects with a 400.
    """
    ids: set[str] = set()
    answered: set[str] = set()
    for msg in group["msgs"]:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                tid = tc.get("id")
                if tid:
                    ids.add(tid)
        elif msg.get("role") == "tool":
            tid = msg.get("tool_call_id")
            if tid:
                answered.add(tid)
    return ids.issubset(answered)


def _pairs_ok(messages: list[dict],
              open_calls: set[str] | None = None) -> bool:
    """
    Safety net: no tool message may appear without its originating tool call.

    ``open_calls`` holds the tool calls that were issued just before this
    region — a call whose assistant message sits in the previous region and
    whose result lands in this one.  Without it, a region that starts with a
    tool result would look like an orphan and could never be compressed.
    """
    pending: set[str] = set(open_calls or ())
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tid = tc.get("id")
                if tid:
                    pending.add(tid)
        elif msg.get("role") == "tool":
            tid = msg.get("tool_call_id")
            if tid not in pending:
                return False
            pending.discard(tid)
    return True


def _open_calls(messages: list[dict]) -> set[str]:
    """The tool calls that are still waiting for their result at the end."""
    pending: set[str] = set()
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tid = tc.get("id")
                if tid:
                    pending.add(tid)
        elif msg.get("role") == "tool":
            tid = msg.get("tool_call_id")
            if tid:
                pending.discard(tid)
    return pending


def _unanswered_calls(messages: list[dict]) -> set[str]:
    """The tool calls that have no matching tool result."""
    calls: set[str] = set()
    answered: set[str] = set()
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tid = tc.get("id")
                if tid:
                    calls.add(tid)
        elif msg.get("role") == "tool":
            tid = msg.get("tool_call_id")
            if tid:
                answered.add(tid)
    return calls - answered


# ══════════════════════════════════════════════════════════════════════════
# Compression levels
# ══════════════════════════════════════════════════════════════════════════

def _tool_names(group: dict) -> list[str]:
    names: list[str] = []
    for msg in group["msgs"]:
        if not isinstance(msg, dict):
            continue
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            name = fn.get("name", "tool") if isinstance(fn, dict) else "tool"
            if name not in names:
                names.append(name)
    return names


def _shorten_call_args(args: str, limit: int) -> str:
    """
    Shorten a tool call's arguments while keeping them valid JSON.

    The provider parses the ``arguments`` of an assistant tool call, so a
    blind character cut would leave malformed JSON behind and the request
    would be rejected — the compression would cause the very failure it is
    meant to prevent.  The *values* are shortened instead, and the result is
    always parseable.
    """
    fallback = {"compressed": True, "original_chars": len(args)}
    try:
        data = json.loads(args)
    except (ValueError, TypeError):
        return json.dumps(fallback, ensure_ascii=False)
    if not isinstance(data, dict):
        return json.dumps(fallback, ensure_ascii=False)

    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            out[key] = (
                val[:limit] + f"…[+{len(val) - limit} chars]"
                if len(val) > limit else val
            )
        elif isinstance(val, (dict, list)):
            blob = json.dumps(val, ensure_ascii=False, default=str)
            out[key] = (
                {"compressed": True, "original_chars": len(blob)}
                if len(blob) > limit else val
            )
        else:
            out[key] = val

    encoded = json.dumps(out, ensure_ascii=False, default=str)
    if len(encoded) > limit * 2:
        # Still too fat — keep the call's identity but drop the values.
        return json.dumps(fallback, ensure_ascii=False)
    return encoded


def _compress_group(group: dict, level: int, archive_id: str | None,
                    protected: set[int] | None = None,
                    open_calls: set[str] | None = None) -> list[dict]:
    """
    Return the compressed form of one pass.

    level 1 — digest:  reasoning dropped, tool arguments and tool outputs
                        truncated to a readable head.
    level 2 — compact: tool arguments replaced by a size marker, tool outputs
                        cut to a short head, prose cut harder.
    level 3 — stub:    the whole pass collapses to a single marker line.
    """
    msgs = group["msgs"]

    # Messages the caller wants left raw (the current prompt, the newest
    # turns).  Their positions are given as indices into the *region*, so
    # they are translated into positions inside this group.
    protected_local: set[int] = set()
    if protected:
        protected_local = {
            i - group["start"] for i in protected
            if group["start"] <= i < group["end"]
        }

    # The stub form destroys the whole pass, so it is only used when the
    # pass holds nothing that must stay raw.
    #
    # A tool result whose call was issued in the previous region is the
    # exception: stubbing it away would leave that call without its answer,
    # and a provider rejects the request.  Such a result is truncated in
    # place instead.
    keeps_open_result = bool(open_calls) and any(
        isinstance(m, dict) and m.get("role") == "tool"
        and m.get("tool_call_id") in open_calls
        for m in msgs
    )

    # The mirror image: a group that carries a tool call whose result is not
    # in the group.  Stubbing it away would leave that result — which lives
    # in another region — without its call.
    local_ids = {
        tc.get("id")
        for m in msgs if isinstance(m, dict)
        for tc in (m.get("tool_calls") or []) if isinstance(tc, dict)
    }
    answered_local = {
        m.get("tool_call_id")
        for m in msgs if isinstance(m, dict) and m.get("role") == "tool"
    }
    keeps_open_call = bool(local_ids - answered_local)

    if (level >= 3 and not protected_local
            and not keeps_open_result and not keeps_open_call):
        names = ", ".join(_tool_names(group))
        if names:
            what = f"compressed pass: {names}"
        else:
            role = (group["msgs"][0].get("role", "message")
                    if group["msgs"] else "message")
            what = f"compressed {role} message"
        raw_chars = _chars_of(msgs)
        where = f"raw archived as {archive_id}" if archive_id else "raw archived"
        head = ""
        if not names:
            content = group["msgs"][0].get("content") if group["msgs"] else None
            if isinstance(content, str) and content.strip():
                head = " — " + content.strip()[:200].replace("\n", " ")
        return [{
            "role": "system",
            "content": (
                f"[{what} — {len(msgs)} message(s), "
                f"{raw_chars:,} chars — {where}]{head}"
            ),
        }]

    out: list[dict] = []
    for offset, msg in enumerate(msgs):
        if not isinstance(msg, dict):
            continue
        if offset in protected_local:
            out.append(msg)
            continue
        c = dict(msg)

        # The reasoning trace is the first thing to go: it is the most
        # expensive and the least useful once the outcome is known.
        for key in ("reasoning", "reasoning_content", "reasoning_details",
                    "_reasoning_provider", "_reasoning_model"):
            c.pop(key, None)

        role = c.get("role")

        if role == "assistant" and c.get("tool_calls"):
            new_tcs = []
            for tc in c["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                tc = dict(tc)
                fn = tc.get("function", {})
                fn = dict(fn) if isinstance(fn, dict) else {}
                args = fn.get("arguments", "")
                if isinstance(args, str):
                    if level == 1:
                        if len(args) > L1_CALL_CHARS:
                            # Shorten the *values*, never the raw string: the
                            # arguments have to stay parseable JSON.
                            fn["arguments"] = _shorten_call_args(
                                args, L1_CALL_CHARS)
                    else:
                        fn["arguments"] = json.dumps({
                            "compressed": True,
                            "original_chars": len(args),
                        })
                tc["function"] = fn
                new_tcs.append(tc)
            c["tool_calls"] = new_tcs

        elif role == "tool":
            content = c.get("content")
            if isinstance(content, str):
                keep = L1_OUTPUT_CHARS if level == 1 else L2_OUTPUT_CHARS
                if len(content) > keep:
                    c["content"] = (
                        content[:keep]
                        + f"\n…[+{len(content) - keep} chars compressed"
                          + (f" — raw archived as {archive_id}]" if archive_id else "]")
                    )
            # A multimodal tool result is a list of parts: the text parts are
            # kept as they are, because the picture markers are small and the
            # image itself is re-attached at wire time.

        else:
            content = c.get("content")
            if isinstance(content, str):
                # A user message carries the instructions — it survives longer
                # than the model's own prose.
                if c.get("role") == "user":
                    keep = L1_USER_CHARS if level == 1 else L2_USER_CHARS
                else:
                    keep = L1_PROSE_CHARS if level == 1 else L2_PROSE_CHARS
                if len(content) > keep:
                    c["content"] = (
                        content[:keep]
                        + f"\n…[+{len(content) - keep} chars compressed]"
                    )

        out.append(c)

    return out


# ══════════════════════════════════════════════════════════════════════════
# Archive
# ══════════════════════════════════════════════════════════════════════════

def _reserve_archive_id() -> str:
    """Allocate the next archive id (stable, monotone, never reused)."""
    global _next_seq
    with _lock:
        aid = f"A{_next_seq}"
        _next_seq += 1
        return aid


def _content_hash(messages: list[dict]) -> str:
    """Stable fingerprint of a set of raw messages."""
    blob = json.dumps(messages, ensure_ascii=False, sort_keys=True,
                      default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _write_archive(archive_id: str, label: str, level: int,
                   messages: list[dict]) -> str | None:
    """
    Append the raw compressed content to the archive.  Never mutates.

    The archive is content-addressed: the same region compressed twice — which
    happens whenever the same history is compressed again on a later turn —
    reuses the existing entry instead of writing a duplicate.  Returns the
    archive id to reference, or None when the content could not be stored.
    """
    digest = _content_hash(messages)
    with _lock:
        for existing_id, meta in _index.items():
            if meta.get("hash") == digest:
                return existing_id

    entry = {
        "id": archive_id,
        "ts": datetime.now().isoformat(),
        "label": label,
        "level": level,
        "chars": _chars_of(messages),
        "hash": digest,
        "messages": messages,
    }
    line = json.dumps(entry, ensure_ascii=False, default=str)
    with _lock:
        try:
            os.makedirs(os.path.dirname(ARCHIVE_FILE), exist_ok=True)
            with open(ARCHIVE_FILE, "ab") as f:
                offset = f.tell()
                f.write((line + "\n").encode("utf-8"))
        except OSError as exc:
            print(f"[Context] Could not archive segment {archive_id}: {exc}")
            return None
        _index[archive_id] = {
            "id": archive_id,
            "ts": entry["ts"],
            "label": label,
            "level": level,
            "chars": entry["chars"],
            "messages": len(messages),
            "seq": _seq_of(archive_id),
            "offset": offset,
            "hash": digest,
        }
    return archive_id


def _read_entry(archive_id: str, meta: dict) -> dict | None:
    """Read one archive entry back from disk using its recorded offset."""
    try:
        with open(ARCHIVE_FILE, "rb") as f:
            f.seek(meta.get("offset", 0))
            line = f.readline().decode("utf-8", "replace").strip()
        if not line:
            return None
        return json.loads(line)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def retrieve_archive(archive_id: str, offset: int = 0,
                     limit: int = DEFAULT_RETRIEVE_CHARS) -> str:
    """
    Return the raw content of a compressed context segment.

    The raw text is paged: a compressed segment can be far larger than the
    context window that triggered the compression, so the tool takes a
    character offset and a limit and reports where the next slice starts.
    """
    aid = str(archive_id).strip()
    _sync_index()
    with _lock:
        meta = _index.get(aid)
    if meta is None:
        return json.dumps({"error": f"Archive '{aid}' not found."})

    entry = _read_entry(aid, meta)
    if entry is None:
        return json.dumps({
            "error": f"Archive '{aid}' could not be read from {ARCHIVE_FILE}.",
        })

    blob = json.dumps(entry.get("messages", []), ensure_ascii=False,
                      indent=2, default=str)
    try:
        off = max(0, int(offset))
    except (TypeError, ValueError):
        off = 0
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = DEFAULT_RETRIEVE_CHARS
    lim = max(500, min(lim, MAX_RETRIEVE_CHARS))

    piece = blob[off:off + lim]
    end = off + len(piece)
    return json.dumps({
        "id": aid,
        "ts": entry.get("ts", ""),
        "label": entry.get("label", ""),
        "level": entry.get("level", 0),
        "total_chars": len(blob),
        "offset": off,
        "returned_chars": len(piece),
        "truncated": end < len(blob),
        "next_offset": end if end < len(blob) else None,
        "content": piece,
    }, ensure_ascii=False)


def list_archives() -> str:
    """List every archived context segment (id, size, description)."""
    _sync_index()
    with _lock:
        items = sorted(_index.values(), key=lambda e: e.get("seq", 0))
    return json.dumps(items, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════════════════
# Region compression
# ══════════════════════════════════════════════════════════════════════════

def _marker_text(archive_id: str, segments: int, raw_chars: int,
                 new_chars: int) -> str:
    return (
        f"[Compressed context {archive_id} — {segments} earlier segment(s) "
        f"(reasoning details, tool calls and tool outputs) compressed from "
        f"{raw_chars:,} to ~{new_chars:,} chars. The raw text is archived: "
        f"call retrieve_archive('{archive_id}') to read it back (it pages in "
        f"slices), then continue the work from there.]"
    )


def _compress_region(messages: list[dict], budget_tokens: int | None = None,
                     forced_level: int | None = None,
                     keep_recent: int = KEEP_RECENT_MESSAGES,
                     protect_first: int = 0,
                     protect_last_user: bool = False,
                     open_calls: set[str] | None = None
                     ) -> tuple[list[dict], dict]:
    """
    Compress the oldest segments of ``messages`` until the estimated payload
    fits ``budget_tokens``.

    ``budget_tokens=None`` means "compress every eligible segment" — that is
    the reactive path used after a real context-length error.

    ``keep_recent``       — how many of the newest messages stay raw.
    ``protect_first``     — how many of the oldest messages stay raw.
    ``protect_last_user`` — keep the last *user* message raw.  That is the
                            prompt being answered; it must never be reduced
                            to a stub.

    Protection is per *message*, not per segment: a segment that straddles
    the protected boundary keeps its protected messages raw and compresses
    the rest.  That way one enormous tool output can be compressed without
    touching the prompt that is being answered.

    Returns ``(new_messages, report)``.
    """
    report = {
        "compressed": False,
        "level": 0,
        "passes": 0,
        "archive_id": None,
        "raw_chars": 0,
        "new_chars": 0,
        "estimated_tokens": estimate_tokens(messages),
        "budget_tokens": budget_tokens,
    }
    if not messages:
        return messages, report

    # 1. The leading system messages are the system prompt and the chunk
    #    summaries — already the compressed form of older work.  Never touch.
    prefix_end = 0
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            prefix_end += 1
        else:
            break
    prefix = messages[:prefix_end]
    body = messages[prefix_end:]

    if not body:
        return messages, report

    # 2. Work out which messages must stay raw.
    # Messages that must never be reduced: the prompt being answered
    # and anything the caller explicitly asked to keep.
    must: set[int] = set()
    if protect_first > 0:
        must.update(range(min(protect_first, len(body))))
    if protect_last_user:
        for i in range(len(body) - 1, -1, -1):
            msg = body[i]
            if isinstance(msg, dict) and msg.get("role") == "user":
                must.add(i)
                break

    protected: set[int] = set(must)

    # The recency protection never swallows the whole region: when the
    # region is no longer than ``keep_recent``, only its newest message
    # stays raw.  Otherwise a short region — three messages holding one
    # enormous tool output, say — could never be compressed at all.
    if keep_recent > 0 and body:
        recent = set(range(max(0, len(body) - keep_recent), len(body)))
        if len(recent) >= len(body):
            recent = {len(body) - 1}
        protected |= recent

    # The recency preference is a nicety, not a rule: when the protected
    # part alone does not fit the budget, it is compressed as well — there
    # is no other way to get under the limit.  The raw text is archived, so
    # nothing is lost: the model can pull it back by id.
    if budget_tokens is not None and protected > must:
        protected_tokens = estimate_tokens(
            [body[i] for i in sorted(protected) if 0 <= i < len(body)]
        )
        if protected_tokens > budget_tokens:
            protected = set(must)

    # 3. Group the body into segments and pick the compressible ones.
    groups = _group_passes(body)
    compressible_idx: list[int] = []
    partial_idx: list[int] = []
    for gi, group in enumerate(groups):
        if all(i in protected for i in range(group["start"], group["end"])):
            continue                  # nothing to gain here
        if _group_is_complete(group):
            compressible_idx.append(gi)
        else:
            # An incomplete segment — a tool call whose result lives outside
            # this region.  It cannot be stubbed away (the call would lose
            # its answer), but its arguments can still be shortened, which is
            # safe because the call itself survives.
            partial_idx.append(gi)

    if not compressible_idx and not partial_idx:
        return messages, report

    protected_prefix_tokens = estimate_tokens(prefix)
    levels = [forced_level] if forced_level else [1, 2, 3]

    for level in levels:
        # A partial group (a tool call whose result lives outside this region)
        # is eligible at every level: _compress_group keeps the call itself
        # and only shortens its arguments, so the pairing survives.
        eligible = sorted(compressible_idx + partial_idx)
        if not eligible:
            continue

        eligible_set = set(eligible)
        suffix_msgs = [m for gi, group in enumerate(groups)
                       if gi not in eligible_set for m in group["msgs"]]
        suffix_tokens = estimate_tokens(suffix_msgs)

        raw_sizes = [
            estimate_tokens(groups[gi]["msgs"]) for gi in eligible
        ]
        comp_sizes = [
            estimate_tokens(_compress_group(groups[gi], level, None,
                                            protected, open_calls))
            for gi in eligible
        ]

        for k in range(1, len(eligible) + 1):
            total = (
                protected_prefix_tokens
                + sum(comp_sizes[:k])
                + sum(raw_sizes[k:])
                + suffix_tokens
                + 70                       # the marker message itself
            )
            # A budget of None means "compress every eligible segment" — that
            # is the reactive path, where the provider already told us the
            # payload did not fit.
            fits = (
                (budget_tokens is None and k == len(eligible))
                or (budget_tokens is not None and total <= budget_tokens)
            )
            if fits:
                chosen = eligible[:k]
                new_msgs, rep = _commit(groups, chosen, level, prefix,
                                        protected, open_calls)
                if new_msgs is not None and _pairs_ok(new_msgs, open_calls):
                    rep["estimated_tokens"] = estimate_tokens(new_msgs)
                    return new_msgs, rep
                # Compression would have broken a pairing — keep the original.
                return messages, report

    # Nothing fit the budget at any level: fall back to the most drastic
    # level over every compressible segment and return the best effort.
    # Partial segments come along: their tool call survives the compression,
    # only its arguments shrink.
    chosen = sorted(compressible_idx + partial_idx)
    if not chosen:
        return messages, report
    new_msgs, rep = _commit(groups, chosen, 3, prefix, protected, open_calls)
    if new_msgs is not None and _pairs_ok(new_msgs, open_calls):
        rep["estimated_tokens"] = estimate_tokens(new_msgs)
        return new_msgs, rep
    return messages, report


def _commit(groups: list[dict], chosen: list[int], level: int,
            prefix: list[dict],
            protected: set[int] | None = None,
            open_calls: set[str] | None = None
            ) -> tuple[list[dict] | None, dict]:
    """
    Archive the raw content of the chosen passes and build the new list.

    Returns ``(new_messages, report)``; ``new_messages`` is None when the
    archive could not be written (in which case nothing is changed — a
    segment that cannot be archived must not be silently dropped).
    """
    raw_messages: list[dict] = []
    for gi in chosen:
        raw_messages.extend(groups[gi]["msgs"])

    archive_id = _reserve_archive_id()
    label = (f"compressed context: {len(chosen)} segment(s), "
             f"{len(raw_messages)} message(s)")
    stored_id = _write_archive(archive_id, label, level, raw_messages)
    if stored_id is None:
        return None, {}
    archive_id = stored_id

    new_body: list[dict] = []
    compressed_new_chars = 0
    for gi, group in enumerate(groups):
        if gi in chosen:
            replacement = _compress_group(group, level, archive_id, protected,
                                         open_calls)
            compressed_new_chars += _chars_of(replacement)
            new_body.extend(replacement)
        else:
            new_body.extend(group["msgs"])

    # The marker goes in front of the first compressed segment.
    marker_index = 0
    for pos, gi in enumerate(groups):
        if gi in chosen:
            marker_index = pos
            break
    first_pos = groups[marker_index]["start"]
    new_body.insert(first_pos, {
        "role": "system",
        "content": _marker_text(
            archive_id,
            len(chosen),
            _chars_of(raw_messages),
            compressed_new_chars,
        ),
    })

    new_messages = list(prefix) + new_body
    report = {
        "compressed": True,
        "level": level,
        "passes": len(chosen),
        "archive_id": archive_id,
        "raw_chars": _chars_of(raw_messages),
        "new_chars": compressed_new_chars,
        "estimated_tokens": estimate_tokens(new_messages),
        "budget_tokens": None,
    }
    return new_messages, report


# ══════════════════════════════════════════════════════════════════════════
# Public entry points
# ══════════════════════════════════════════════════════════════════════════

def fit_to_budget(messages: list[dict], budget_tokens: int,
                  head_len: int | None = None,
                  keep_recent: int = KEEP_RECENT_MESSAGES
                  ) -> tuple[list[dict], int, dict]:
    """
    Fit a message list inside ``budget_tokens`` by compressing its oldest
    passes.

    ``head_len`` marks the boundary between the history region (older turns)
    and the live turn (the messages produced by the current request).  The
    two regions are compressed separately so the live turn never merges with
    the history, and the returned head length keeps that boundary intact for
    the caller (``set_tool_context`` slices on it).

    The live turn therefore starts at the returned head length — but when the
    live turn itself was compressed, its first message is the compression
    marker rather than the original one.  The marker is a system message, so
    the tool-context filter ignores it.

    Returns ``(messages, head_len, report)``.
    """
    if not messages:
        return messages, head_len or 0, {"compressed": False}

    if head_len is None:
        head_len = len(messages)
    head_len = max(0, min(head_len, len(messages)))

    if estimate_tokens(messages) <= budget_tokens:
        return messages, head_len, {
            "compressed": False,
            "estimated_tokens": estimate_tokens(messages),
            "budget_tokens": budget_tokens,
        }

    head = messages[:head_len]
    tail = messages[head_len:]

    # The turn in progress has priority: the history absorbs the compression
    # so the live turn stays raw.  The live turn only gives way when it does
    # not fit the budget on its own — one enormous tool output can be the
    # real culprit.
    tail_tokens = estimate_tokens(tail)
    # The head's share of the budget.  The floor keeps it from collapsing to
    # nothing when the live turn alone fills the window — but it is small on
    # purpose: when the live turn is that big, the history has to give way
    # hard, and every token over the budget is one the provider may reject.
    head_budget = max(256, budget_tokens - tail_tokens)

    new_head, rep_head = _compress_region(head, budget_tokens=head_budget,
                                          keep_recent=keep_recent,
                                          protect_last_user=True)
    if not rep_head.get("compressed"):
        new_head, rep_head = _compress_region(head, budget_tokens=None,
                                              keep_recent=keep_recent,
                                              protect_last_user=True)

    # A tool call issued in the history and answered in the live turn spans
    # the boundary.  The live turn must know about it, otherwise its tool
    # result looks like an orphan and can never be compressed.
    open_calls = _open_calls(new_head)

    remaining = budget_tokens - estimate_tokens(new_head)
    new_tail, rep_tail = tail, {}
    if remaining < estimate_tokens(tail):
        new_tail, rep_tail = _compress_region(tail, budget_tokens=remaining,
                                              keep_recent=TAIL_KEEP_RECENT,
                                              protect_first=TAIL_PROTECT_FIRST,
                                              open_calls=open_calls)
        if not rep_tail.get("compressed"):
            new_tail, rep_tail = _compress_region(tail, budget_tokens=None,
                                                  keep_recent=TAIL_KEEP_RECENT,
                                                  protect_first=TAIL_PROTECT_FIRST,
                                                  open_calls=open_calls)

    new_messages = new_head + new_tail

    # The two regions are checked together: compression must not orphan a
    # tool result, and it must not leave a tool call without its answer —
    # a provider rejects both.
    if not _pairs_ok(new_messages) or not (
        _unanswered_calls(new_messages) <= _unanswered_calls(messages)
    ):
        return messages, head_len, {
            "compressed": False,
            "estimated_tokens": estimate_tokens(messages),
            "budget_tokens": budget_tokens,
        }
    report = {
        "compressed": bool(rep_head.get("compressed")
                           or rep_tail.get("compressed")),
        "level": max(rep_head.get("level", 0), rep_tail.get("level", 0)),
        "passes": rep_head.get("passes", 0) + rep_tail.get("passes", 0),
        "archive_ids": [
            a for a in (rep_head.get("archive_id"), rep_tail.get("archive_id"))
            if a
        ],
        "estimated_tokens": estimate_tokens(new_messages),
        "budget_tokens": budget_tokens,
    }
    if not report["compressed"]:
        return messages, head_len, report
    return new_messages, len(new_head), report


def compress_forced(messages: list[dict], head_len: int | None = None,
                    level: int = 2,
                    protect_prompt: bool = True
                    ) -> tuple[list[dict], int, dict]:
    """
    Compress the context without a budget — the reactive path, used after a
    provider reported a real context-length error.

    This is the emergency path, so it compresses everything it is allowed to:
    the newest turns and the live turn's tool outputs included.  Only the
    prompt being answered is held back (``protect_prompt``) — everything else
    is archived and can be pulled back by id.

    ``level=2`` keeps a usable digest of every segment; ``level=3`` collapses
    them to stubs.  The raw text is archived either way.
    """
    if not messages:
        return messages, head_len or 0, {"compressed": False}
    if head_len is None:
        head_len = len(messages)
    head_len = max(0, min(head_len, len(messages)))

    head = messages[:head_len]
    tail = messages[head_len:]

    new_head, rep_head = _compress_region(
        head, budget_tokens=None, forced_level=level,
        keep_recent=0, protect_last_user=protect_prompt,
    )
    open_calls = _open_calls(new_head)
    new_tail, rep_tail = _compress_region(
        tail, budget_tokens=None, forced_level=level,
        keep_recent=0, protect_first=TAIL_PROTECT_FIRST,
        open_calls=open_calls,
    )

    new_messages = new_head + new_tail

    if not _pairs_ok(new_messages) or not (
        _unanswered_calls(new_messages) <= _unanswered_calls(messages)
    ):
        return messages, head_len, {"compressed": False}
    report = {
        "compressed": bool(rep_head.get("compressed")
                           or rep_tail.get("compressed")),
        "level": level,
        "passes": rep_head.get("passes", 0) + rep_tail.get("passes", 0),
        "archive_ids": [
            a for a in (rep_head.get("archive_id"), rep_tail.get("archive_id"))
            if a
        ],
        "estimated_tokens": estimate_tokens(new_messages),
    }
    if not report["compressed"]:
        return messages, head_len, report
    return new_messages, len(new_head), report
