"""core/spill.py — oversized tool results go to disk, not into the context.

A tool result joins the model's context the moment it is appended to the
message list, and it stays there for the rest of the conversation.
``browse_web`` alone can hand back 50,000 characters; a shell command can
hand back anything at all.  Pushing that verbatim into the window is how a
session dies of context exhaustion while the useful part was three lines
near the top.

So the oversized result is written to ``data/spill/<session>/`` and the model
receives the head, the tail and the path.  The rest is still there —
``read_file`` and ``search_in_files`` reach it — but it costs nothing until
somebody asks for it.

Two rules make this safe:

* **A read of a spill file is never spilled again.**  Otherwise the model
  would follow a pointer to another pointer and never see the content it
  asked for.  The exemption is decided from the *arguments* of the call,
  not from the content: the content of a spill file is arbitrary text and
  cannot be recognised by looking at it.
* **A failed write leaves the original result inline.**  The spill is an
  optimisation; it must never turn a working tool call into a lost one.
"""
from __future__ import annotations

import os
import re
import secrets
import time

import paths
from core import app_config

#: Rough characters-per-token ratio used for the size estimate.  The same
#: crude division the rest of the runtime uses (``len(payload) // 4``): the
#: number only has to be good enough to decide "too big to inline".
CHARS_PER_TOKEN = 4

#: A tool result larger than this is written to disk instead of being sent
#: inline.  Roughly 16 KB — big enough that a normal command, a short file
#: listing or a moderate web page still arrives whole, small enough that a
#: runaway output cannot eat the window.
#:
#: The value lives in ``app_config.DEFAULT_CONFIG`` — that is the one place
#: the shape of ``config.json`` is defined, and a second copy here would be
#: a second answer to the same question.
SPILL_TRIGGER_TOKENS = int(app_config.DEFAULT_CONFIG["spill_trigger_tokens"])

#: How much of a spilled result stays inline at most — the beginning, the
#: end, and the notice in between.  The middle is what the model can ask for
#: by name once it knows the path.  The budget is never larger than the
#: spill threshold, so the head and the tail can never overlap.
INLINE_BUDGET_CHARS = 12000

#: How the inline budget is split: the beginning carries the context (what
#: the call was, how it started), the end carries the result (the summary
#: line, the error, the last rows).
INLINE_HEAD_RATIO = 0.6

#: How long a spill session is kept on disk.  A spill file exists so the
#: model can fetch the part it needs; a week is long enough for a
#: multi-day task and short enough that the phone's storage is not eaten
#: by yesterday's web pages.
SPILL_RETENTION_DAYS = 7

#: Tools whose *target* is a path, mapped to the argument holding it.
#: A call that reads inside the spill directory is exempt from spilling.
_SPILL_PATH_TOOLS = {
    "read_file": "path",
    "search_in_files": "path",
    "list_directory": "path",
}

_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

_session_label: str | None = None
_trigger_cache: tuple[float, int, bool] | None = None


def _safe_name(name: str) -> str:
    """A file-name fragment that cannot escape the spill directory."""
    cleaned = _UNSAFE_NAME_RE.sub("_", str(name or "tool")).strip("._-")
    return cleaned[:40] or "tool"


def session_label() -> str:
    """The label of the current spill session.

    One directory per running process: two sessions never share a spill
    file, and the name says when the session started and which process owns
    it.
    """
    global _session_label
    if _session_label is None:
        _session_label = f"{time.strftime('%Y%m%d-%H%M%S')}-p{os.getpid()}"
    return _session_label


def session_dir() -> str:
    """The directory this session's spill files are written to."""
    return os.path.join(paths.SPILL_DIR, session_label())


def estimate_tokens(text: str) -> int:
    """Rough token count of *text* (characters / 4)."""
    if not text:
        return 0
    return len(text) // CHARS_PER_TOKEN


def _resolved_path(path: str) -> str:
    """The absolute path a tool argument refers to."""
    expanded = os.path.expanduser(str(path or ""))
    if not os.path.isabs(expanded):
        expanded = os.path.join(paths.ROOT, expanded)
    return os.path.abspath(expanded)


def is_spill_path(path: str) -> bool:
    """Whether *path* points inside the spill directory."""
    if not path:
        return False
    try:
        target = _resolved_path(path)
        root = os.path.abspath(paths.SPILL_DIR)
        return os.path.commonpath([target, root]) == root
    except (ValueError, OSError):
        # Different drives on Windows, or an unreadable path: not inside.
        return False


def reads_spill_dir(tool_name: str, args: dict | None) -> bool:
    """Whether this call reads from the spill directory.

    A read of a spill file is exempt from spilling: the model asked for
    that content on purpose, and replacing it with another pointer would
    be a loop rather than a saving.
    """
    argument = _SPILL_PATH_TOOLS.get(str(tool_name or ""))
    if not argument or not isinstance(args, dict):
        return False
    return is_spill_path(args.get(argument, ""))


def _settings() -> tuple[int, bool]:
    """The spill threshold and the on/off switch, read from the config.

    The config file is re-read only when it changes, so the threshold can be
    tuned (or spilling switched off) without restarting the session.
    """
    global _trigger_cache
    try:
        mtime = os.path.getmtime(paths.CONFIG_FILE)
    except OSError:
        mtime = 0.0

    if _trigger_cache and _trigger_cache[0] == mtime:
        return _trigger_cache[1], _trigger_cache[2]

    tokens = SPILL_TRIGGER_TOKENS
    enabled = True
    try:
        config = app_config.load_config()
        tokens = int(config.get("spill_trigger_tokens", SPILL_TRIGGER_TOKENS))
        enabled = bool(config.get("spill_enabled", True))
    except Exception:
        tokens = SPILL_TRIGGER_TOKENS
        enabled = True
    tokens = max(0, tokens)

    _trigger_cache = (mtime, tokens, enabled)
    return tokens, enabled


def should_spill(tool_name: str, args: dict | None, text: str) -> bool:
    """Whether this tool result is too big to keep inline."""
    if not text or not isinstance(text, str):
        return False
    tokens, enabled = _settings()
    if not enabled or tokens <= 0:
        return False
    if len(text) <= tokens * CHARS_PER_TOKEN:
        return False
    if reads_spill_dir(tool_name, args):
        return False
    return True


def _prune_old_sessions(max_age_days: int = SPILL_RETENTION_DAYS) -> None:
    """Delete the spill directories of sessions that are long gone.

    A spill file is transient by nature — it exists so the model can fetch
    the part it needs — but on a phone the storage is small and nobody
    remembers to clean it.  The directories are named after the session that
    wrote them, so age is enough to recognise the stale ones.
    """
    cutoff = time.time() - (max_age_days * 86400)
    try:
        entries = os.listdir(paths.SPILL_DIR)
    except OSError:
        return
    for name in entries:
        directory = os.path.join(paths.SPILL_DIR, name)
        if not os.path.isdir(directory):
            continue
        try:
            if os.path.getmtime(directory) >= cutoff:
                continue
        except OSError:
            continue
        try:
            for entry in os.listdir(directory):
                try:
                    os.remove(os.path.join(directory, entry))
                except OSError:
                    pass
            os.rmdir(directory)
        except OSError:
            pass


def _write_spill_file(directory: str, filename: str, text: str) -> str:
    """Write *text* to a fresh private file and return its path."""
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass

    path = os.path.join(directory, filename)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as handle:
        handle.write(text)
    return path


def _relative(path: str) -> str:
    """The path as the model should see it — relative to the project root."""
    try:
        return os.path.relpath(path, paths.ROOT)
    except ValueError:
        return path


def _head_tail(text: str, budget: int) -> tuple[str, str]:
    """Split *text* into a head and a tail that do not overlap.

    The head carries the beginning (what the call was, how it started), the
    tail carries the end (the summary line, the error, the last rows).  The
    middle is what the model asks for by name once it knows the path.
    """
    if budget <= 0 or len(text) <= budget:
        return text, ""
    head_len = int(budget * INLINE_HEAD_RATIO)
    tail_len = budget - head_len
    return text[:head_len], text[-tail_len:] if tail_len else ""


def spill_result(
    tool_name: str,
    text: str,
    args: dict | None = None,
) -> str:
    """Return the inline form of *text*, spilling it to disk when it is big.

    The full text is always written to the spill file first; the returned
    string carries the head, a notice that names the file and how to read
    it, and the tail.  If anything goes wrong on the way, the original text
    is returned unchanged — a working tool result must never disappear.
    """
    if not should_spill(tool_name, args, text):
        return text

    directory = session_dir()
    filename = f"{secrets.token_hex(4)}-{_safe_name(tool_name)}.txt"
    try:
        path = _write_spill_file(directory, filename, text)
    except Exception:
        # The spill is an optimisation.  If it fails, the result stays whole.
        return text
    _prune_old_sessions()

    relative = _relative(path)
    tokens, _enabled = _settings()
    budget = min(tokens * CHARS_PER_TOKEN, INLINE_BUDGET_CHARS)
    head, tail = _head_tail(text, budget)
    omitted = max(0, len(text) - len(head) - len(tail))

    notice = (
        f"\n\n[SPILLED] {omitted:,} characters of this result were written to "
        f"disk instead of being sent inline.\n"
        f"Full output: {relative}\n"
        f"Read it with read_file(path=\"{relative}\"), or grep it with "
        f"search_in_files(path=\"{os.path.dirname(relative)}\", query=\"…\").\n"
    )

    return head + notice + tail
