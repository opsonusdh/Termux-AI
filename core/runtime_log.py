"""core/runtime_log.py — the real-time session log.

Everything the runtime does is written to ``logs/log.txt`` *as it happens*:

* what the user typed,
* what the model was thinking (provider reasoning and mid-turn narration),
* every tool call with its full arguments,
* every tool result with its full output,
* the delegation traffic (the manager's command, the sub-AI's tool calls and
  their results),
* the final replies.

Two properties matter here, and both are deliberate:

**Real time.**  Every write is flushed to disk immediately, so
``tail -f logs/log.txt`` shows the work while it is happening.  A tool call
is logged *before* it runs and its result *after* it returns, so a session
killed in the middle of a turn still leaves a trail of what was being done.
Nothing is batched until the end of the turn.

**Bounded.**  The file is capped at :data:`MAX_LINES` lines.  When it grows
past the cap the oldest lines are dropped — one after another, oldest first —
so the log keeps the most recent history instead of eating the device's
storage.  The trim rewrites the file through a temporary file and replaces it
atomically, so a reader tailing the log never sees a half-written file.

The module depends on nothing but the standard library and ``paths``, so any
other module can import it without creating a cycle.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime

import paths

# The hard cap on the number of lines the log may hold.
MAX_LINES = 1_000_000

# How often the line count is recomputed from the file itself, so a second
# Termux-AI session writing to the same log is noticed.
_RECOUNT_EVERY = 25_000

_lock = threading.RLock()
_line_count: int | None = None
_writes_since_recount = 0


# ── Low-level writer ────────────────────────────────────────────────────────

def stamp() -> str:
    """Timestamp used on every log line: ``2026-11-03 16:40:01``."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_path() -> str:
    """Absolute path of the session log."""
    return paths.LOG_FILE


def _count_lines(path: str) -> int:
    """Count the lines of *path* without loading the file into memory."""
    if not os.path.exists(path):
        return 0
    total = 0
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                total += chunk.count(b"\n")
        # A file whose last line has no trailing newline still has that line.
        try:
            if os.path.getsize(path) > 0:
                with open(path, "rb") as fh:
                    fh.seek(-1, os.SEEK_END)
                    if fh.read(1) != b"\n":
                        total += 1
        except OSError:
            pass
    except OSError:
        return total
    return total


def _trim_one_line_locked() -> None:
    """Drop exactly the oldest line to bring the file back under MAX_LINES.

    Called with ``_lock`` held.  The file is streamed through a temporary
    file and swapped in atomically, so a concurrent reader never observes a
    partially trimmed log.
    """
    global _line_count

    path = paths.LOG_FILE
    if _line_count is None:
        _line_count = _count_lines(path)
    if _line_count <= MAX_LINES:
        return

    tmp = path + ".trim"
    removed = 0
    kept = 0
    last_line_ended_with_newline = True
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as src, \
             open(tmp, "w", encoding="utf-8", errors="replace") as dst:
            for line in src:
                if removed == 0:
                    removed = 1
                    continue
                dst.write(line)
                kept += 1
                last_line_ended_with_newline = line.endswith("\n")
            if kept and not last_line_ended_with_newline:
                dst.write("\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return

    _line_count = kept


def _write(payload: str) -> None:
    """Append *payload* to the log and flush it immediately."""
    global _line_count, _writes_since_recount

    with _lock:
        try:
            os.makedirs(paths.LOGS_DIR, exist_ok=True)
            if _line_count is None:
                _line_count = _count_lines(paths.LOG_FILE)
            with open(paths.LOG_FILE, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(payload)
                fh.flush()
            _line_count += payload.count("\n")
            _writes_since_recount += 1
            if _writes_since_recount >= _RECOUNT_EVERY:
                _writes_since_recount = 0
                _line_count = _count_lines(paths.LOG_FILE)
            # Strict 1M cap: trim one line at a time when over the limit.
            while _line_count > MAX_LINES:
                _trim_one_line_locked()
        except Exception:
            # The log must never break the runtime.
            pass


# ── Public writers ──────────────────────────────────────────────────────────

def log_line(text: str) -> None:
    """Append a single pre-formatted line to the log."""
    if text is None:
        return
    if not isinstance(text, str):
        text = str(text)
    _write(text.rstrip("\n") + "\n")


def log_event(kind: str, body: str = "", header: str = "", indent: int = 4) -> None:
    """Write one event: a tagged header line plus an indented body.

    ``kind`` names the kind of event (``tool:call``, ``reasoning``, …),
    ``header`` is an optional short label on the same line, and ``body`` is
    the payload — indented underneath so the structure stays readable.
    """
    head = f"[{stamp()}] [{kind}]"
    if header:
        head += f" {header}"
    text = body if isinstance(body, str) else str(body or "")
    if text.strip():
        pad = " " * indent
        lines = text.split("\n")
        while lines and lines[-1] == "":
            lines.pop()
        block = "\n".join(pad + line if line else "" for line in lines)
        _write(head + "\n" + block + "\n")
        return
    _write(head + "\n")


def format_args(args: object) -> str:
    """Render tool arguments as readable JSON."""
    if args is None:
        return ""
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except (TypeError, ValueError):
            return args
    if not isinstance(args, dict) or not args:
        return ""
    try:
        return json.dumps(args, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(args)


# ── Domain events ───────────────────────────────────────────────────────────

def log_user_input(text: str, source: str = "chat") -> None:
    """The user's message, logged the moment it is received."""
    log_event("user", text or "", header=f"input | {source}")


def log_assistant_reply(text: str, voice: bool = False, agent: bool = False) -> None:
    """The model's final reply, logged as soon as it arrives."""
    if agent:
        kind, header = "agent-reply", "final"
    elif voice:
        kind, header = "voice-reply", "final"
    else:
        kind, header = "reply", "final"
    log_event(kind, text or "", header=header)


def log_reasoning(provider: str, model: str, text: str) -> None:
    """Provider-native reasoning, logged when it arrives."""
    log_event("reasoning", text or "", header=f"{provider}/{model}")


def log_working_trace(text: str) -> None:
    """The model's mid-turn narration, logged when it is produced."""
    log_event("working-trace", text or "")


def log_tool_call(name: str, args: object = None) -> None:
    """A tool call, logged *before* the tool runs."""
    body = format_args(args)
    log_event("tool:call", body, header=str(name))


def log_tool_result(name: str, result: object = "", elapsed: float | None = None,
                    error: bool = False) -> None:
    """A tool result, logged as soon as the tool returns."""
    if isinstance(result, (dict, list)):
        body = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    else:
        body = "" if result is None else str(result)
    kind = "tool:error" if error else "tool:result"
    header = str(name)
    if elapsed is not None:
        header += f" ({elapsed:.2f}s)"
    log_event(kind, body, header=header)


def log_delegate_request(task: str, context: str = "", intelligence: str = "",
                         tools: list | None = None, iterations: int = 0,
                         system_prompt: str | None = None) -> None:
    """The command the manager AI handed to a sub-AI."""
    parts = [f"intelligence={intelligence}", f"iterations={iterations}"]
    if tools:
        parts.append("tools=" + ", ".join(str(t) for t in tools))
    header = "delegate | " + " | ".join(parts)
    body = f"task:\n{task or ''}"
    if context:
        body += f"\n\ncontext:\n{context}"
    if system_prompt:
        body += f"\n\nsystem prompt:\n{system_prompt}"
    log_event("delegate:request", body, header=header)


def log_delegate_tool_call(name: str, args: object = None) -> None:
    """A tool call made by the delegated sub-AI."""
    log_event("delegate:tool:call", format_args(args), header=str(name))


def log_delegate_tool_result(name: str, result: object = "") -> None:
    """The result of a tool call made by the delegated sub-AI."""
    if isinstance(result, (dict, list)):
        body = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    else:
        body = "" if result is None else str(result)
    log_event("delegate:tool:result", body, header=str(name))


def log_delegate_report(text: str, model: str = "", intelligence: str = "") -> None:
    """The final report a sub-AI handed back to the manager."""
    header = f"{model} | {intelligence}".strip(" |")
    log_event("delegate:report", text or "", header=header)


def log_note(text: str, kind: str = "note") -> None:
    """A free-form note (session markers, errors, lifecycle events)."""
    log_event(kind, text or "")


def reset_state() -> None:
    """Forget the cached line count.

    The count is only a cache: the file on disk is the truth.  It has to be
    dropped when the log file is replaced from the outside (a rotation, a
    test) so the next write re-counts instead of trusting a stale number.
    """
    global _line_count, _writes_since_recount
    with _lock:
        _line_count = None
        _writes_since_recount = 0


def line_count() -> int:
    """Number of lines currently in the log (re-counted from disk)."""
    with _lock:
        return _count_lines(paths.LOG_FILE)