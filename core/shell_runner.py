"""
core/shell_runner.py — shell execution with per-stage timeouts and guaranteed
process-tree termination.

Why this module exists
----------------------
``core.tools.shell()`` used to run the whole command string under a single
Python-level ``communicate(timeout=...)`` watchdog.  When that watchdog fired it
called ``Popen.kill()``, which signals **only** the immediate child
(``bash -c ...``).  Two concrete bugs followed from that:

1. **Orphans.**  Every process the command spawned survived the timeout,
   reparented to init.  A hung ``python build.py`` or ``npm install`` kept
   burning CPU and battery long after the tool reported ``[TIMEOUT]``.
2. **Hangs.**  ``Popen.communicate()`` reads until EOF on the stdout/stderr
   pipes.  If a grandchild inherited those pipes, the post-kill ``communicate()``
   blocked until that grandchild exited — the tool call could hang for minutes
   instead of returning at the timeout.

On top of that, the timeout applied to the chain *as a whole*.  A command like
``cd Termux-AI/ && python build.py`` shared one budget, so a slow first stage
starved the rest, and only the first command was effectively bounded.

What this module does
---------------------
* :func:`split_chain` splits a command into the individual stages of an
  ``&&`` / ``||`` / ``;`` / ``|`` chain.  It is quote-aware and
  compound-aware: ``for``/``while``/``if``/``case`` blocks and ``{ }`` groups
  are kept intact instead of being torn apart at their inner semicolons.
* :func:`build_timed_command` rewrites the chain so every *external* stage is
  wrapped in GNU ``timeout``, giving each stage its own budget and making
  GNU ``timeout`` kill that stage's whole process group.  Shell builtins
  (``cd``, ``export``, ``source`` …) are deliberately **not** wrapped: GNU
  ``timeout`` can only exec real binaries, and ``cd`` must keep running in the
  parent shell so that it still affects the following stages.
* :func:`run` executes the command in a dedicated process group and, when the
  budget expires, kills the **entire** process tree (process group + a
  ``/proc`` session sweep) so nothing is left running.

The permission gate is *not* affected: ``core.permissions.validate_command()``
still inspects the original command string.  The rewriting only ever adds a
timeout wrapper around a stage, it never changes what the stage does.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass


# ── Tunables ─────────────────────────────────────────────────────────────────

DEFAULT_STAGE_TIMEOUT = 30
#: Extra seconds the outer watchdog waits past the summed stage budgets before
#: it tears the tree down.  It only ever fires for stages that could not be
#: wrapped in GNU ``timeout`` (shell builtins, compound blocks).
BUDGET_GRACE_SECONDS = 5
#: Hard ceiling for the outer watchdog, so a huge ``timeout`` value combined
#: with a long chain can never block the chat loop indefinitely.
MAX_TOTAL_BUDGET = 1800
#: How long to wait for the pipe readers to drain after the process is gone.
READER_JOIN_GRACE = 3.0
#: Grace period between SIGTERM and SIGKILL for the process group.
KILL_GRACE_SECONDS = 5.0

#: GNU ``timeout`` exit status used when the managed command timed out.
TIMEOUT_EXIT_CODE = 124
#: Upper bound on the output captured from a command.  The stream is still
#: drained to EOF, so the child never blocks on a full pipe — but a runaway
#: ``yes`` cannot exhaust the device's memory either.
MAX_OUTPUT_BYTES = 5 * 1024 * 1024


# ── Shell vocabulary ─────────────────────────────────────────────────────────

#: Bash builtins.  These cannot be wrapped in GNU ``timeout`` (it can only
#: exec real binaries) and several of them (``cd``, ``export``, ``source``)
#: must keep running in the parent shell so their effect survives into the
#: following stages of the chain.
SHELL_BUILTINS = frozenset({
    ".", ":", "[", "alias", "bg", "bind", "break", "builtin", "caller",
    "cd", "command", "compgen", "complete", "compopt", "continue", "declare",
    "dirs", "disown", "echo", "enable", "eval", "exec", "exit", "export",
    "fc", "fg", "getopts", "hash", "help", "history", "jobs", "kill", "let",
    "local", "logout", "mapfile", "popd", "printf", "pushd", "pwd", "read",
    "readarray", "readonly", "return", "set", "shift", "shopt", "source",
    "suspend", "test", "times", "trap", "type", "typeset", "ulimit", "umask",
    "unalias", "unset", "wait",
})

#: Compound keywords and the keyword that terminates them.  A stage that opens
#: one of these blocks is kept whole by :func:`split_chain`.
_COMPOUND_TERMINATORS = {
    "for": "done",
    "while": "done",
    "until": "done",
    "select": "done",
    "if": "fi",
    "case": "esac",
}

#: Leading ``NAME=value`` assignments, e.g. ``PYTHONPATH=. python x.py``.
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: Bash keywords and compound constructs.  These are interpreted by the shell
#: itself, so GNU ``timeout`` cannot exec them — wrapping them would break the
#: command (``timeout 30 for i in ...`` fails with "command not found").
SHELL_KEYWORDS = frozenset({
    "for", "while", "until", "select", "if", "case", "then", "else", "elif",
    "do", "done", "fi", "esac", "time", "!", "{", "}", "[[", "]]", "((", "))",
    "function", "in",
})

#: A command word that looks like a real program path.  Anything else (a shell
#: keyword, a redirection, a compound construct) is left unwrapped.
_COMMAND_WORD_RE = re.compile(r"^[A-Za-z0-9_./~][A-Za-z0-9_./+@=-]*$")

#: File-descriptor duplication such as ``2>&1`` — a single word, no separate
#: redirection target.
_FD_DUP_RE = re.compile(r"^\d*>&")
#: A redirection operator without an attached target (``>``, ``2>>``, ``&>``) —
#: the target is the following word.
_REDIRECT_RE = re.compile(r"^\d*(&>>|&>|>>|>|<)$")
#: A redirection operator with an attached target (``>out.txt``).
_REDIRECT_ATTACHED_RE = re.compile(r"^\d*(&>>|&>|>>|>|<)\S")
#: A shell function definition, e.g. ``build() { ... }``.
_FUNCTION_DEF_RE = re.compile(r"^\s*[\w.-]+\s*\(\s*\)")


def _heredoc_end(cmd: str, pos: int) -> int:
    """Return the index just past the end of a heredoc body.

    *pos* must point just after the ``<<`` / ``<<-`` operator.  When no valid
    heredoc can be located, *pos* is returned unchanged so the caller can fall
    back to normal parsing.
    """
    n = len(cmd)
    i = pos
    while i < n and cmd[i].isspace():
        i += 1
    if i < n and cmd[i] in "'\"":
        quote = cmd[i]
        i += 1
        start = i
        while i < n and cmd[i] != quote:
            i += 1
        delim = cmd[start:i]
        i += 1
    else:
        start = i
        while i < n and not cmd[i].isspace() and cmd[i] not in ";|&":
            i += 1
        delim = cmd[start:i]
    if not delim:
        return pos

    while i < n and cmd[i] != "\n":
        i += 1
    if i < n:
        i += 1

    while i < n:
        eol = cmd.find("\n", i)
        if eol == -1:
            eol = n
        if cmd[i:eol].strip() == delim:
            return eol + 1 if eol < n else n
        i = eol + 1
    return n


# ── Command splitting ────────────────────────────────────────────────────────

def split_chain(cmd: str) -> list[tuple[str, str]]:
    """Split *cmd* into ``(stage, operator)`` pairs.

    The operator is the text that separated the stage from the next one
    (``"&&"``, ``"||"``, ``";"``, ``"|"``, ``"\\n"``) or ``""`` for the last
    stage.  Re-joining the pairs with a single space reproduces a command with
    the same semantics.

    The splitter is quote-aware (single quotes, double quotes, backslash
    escapes, ``$( )`` and backtick substitutions are never split) and
    compound-aware (``for``/``while``/``until``/``select``/``if``/``case``
    blocks and ``{ }`` / ``( )`` groups are kept intact, so the semicolons
    inside a loop body do not split it).

    A single ``&`` (background) *is* a split point and is reported as the
    ``"&"`` operator: in shell syntax ``&`` terminates a command, so
    ``sleep 1 & echo after`` is two commands, and keeping them apart is what
    lets the backgrounded job receive its own timeout.
    """
    stages: list[tuple[str, str]] = []
    buf: list[str] = []
    quote: str | None = None
    escape = False
    i = 0
    n = len(cmd)

    compound: list[str] = []   # stack of expected terminators
    nesting = 0                # ( ) and { } nesting depth
    stmt_start = True          # the next word begins a statement

    def flush(op: str) -> None:
        text = "".join(buf).strip()
        buf.clear()
        if text:
            stages.append((text, op))

    def read_word(pos: int) -> tuple[str, int]:
        j = pos
        while j < n and (cmd[j].isalnum() or cmd[j] in "_-."):
            j += 1
        return cmd[pos:j], j

    while i < n:
        ch = cmd[i]

        # -- escapes ---------------------------------------------------
        if escape:
            buf.append(ch)
            escape = False
            i += 1
            stmt_start = False
            continue
        if ch == "\\":
            buf.append(ch)
            escape = True
            i += 1
            continue

        # -- quoted regions ---------------------------------------------
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            stmt_start = False
            continue
        if ch in ("'", '"'):
            buf.append(ch)
            quote = ch
            i += 1
            stmt_start = False
            continue

        # -- backtick substitution --------------------------------------
        if ch == "`":
            buf.append(ch)
            i += 1
            while i < n:
                buf.append(cmd[i])
                if cmd[i] == "`":
                    i += 1
                    break
                i += 1
            stmt_start = False
            continue

        # -- $( ) command substitution ----------------------------------
        if ch == "$" and cmd.startswith("$(", i):
            buf.append("$(")
            i += 2
            depth = 1
            while i < n and depth > 0:
                if cmd[i] == "(":
                    depth += 1
                elif cmd[i] == ")":
                    depth -= 1
                buf.append(cmd[i])
                i += 1
            stmt_start = False
            continue

        # -- comments ---------------------------------------------------
        if ch == "#" and stmt_start:
            while i < n and cmd[i] != "\n":
                buf.append(cmd[i])
                i += 1
            continue

        # -- here-documents / here-strings --------------------------------
        if cmd.startswith("<<<", i):
            # A here-string (`<<< "text"`) is a single word, not a heredoc.
            buf.append("<<<")
            i += 3
            stmt_start = False
            continue
        if cmd.startswith("<<", i):
            # The heredoc body runs to the delimiter line and must be copied
            # verbatim — splitting it at its newlines would corrupt it.
            marker = "<<-" if cmd.startswith("<<-", i) else "<<"
            buf.append(marker)
            i += len(marker)
            end = _heredoc_end(cmd, i)
            if end > i:
                buf.append(cmd[i:end])
                i = end
            stmt_start = True
            continue

        # -- grouping ---------------------------------------------------
        if ch in "({":
            nesting += 1
            buf.append(ch)
            i += 1
            stmt_start = (ch == "{")
            continue
        if ch in ")}":
            nesting = max(0, nesting - 1)
            buf.append(ch)
            i += 1
            stmt_start = False
            continue

        # -- compound keywords -------------------------------------------
        if stmt_start and (ch.isalpha() or ch == "_"):
            word, j = read_word(i)
            if word in _COMPOUND_TERMINATORS:
                compound.append(_COMPOUND_TERMINATORS[word])
            elif word in ("fi", "esac", "done") and compound and compound[-1] == word:
                compound.pop()
            buf.append(word)
            i = j
            stmt_start = False
            continue

        # -- operators ---------------------------------------------------
        splittable = (not compound) and nesting == 0

        if ch == ";":
            if splittable:
                flush(";")
            else:
                buf.append(ch)
            i += 1
            stmt_start = True
            continue
        if ch == "\n":
            if splittable:
                flush("\n")
            else:
                buf.append(ch)
            i += 1
            stmt_start = True
            continue
        if cmd.startswith("&&", i):
            if splittable:
                flush("&&")
            else:
                buf.append("&&")
            i += 2
            stmt_start = True
            continue
        if cmd.startswith("||", i):
            if splittable:
                flush("||")
            else:
                buf.append("||")
            i += 2
            stmt_start = True
            continue
        if cmd.startswith("|&", i):
            if splittable:
                flush("|&")
            else:
                buf.append("|&")
            i += 2
            stmt_start = True
            continue
        if ch == "|":
            if splittable:
                flush("|")
            else:
                buf.append(ch)
            i += 1
            stmt_start = True
            continue
        if ch == "&":
            # `&` is the background operator — unless it is part of a
            # redirection (`&>`, `&>>`) or a file-descriptor duplication
            # (`2>&1`), in which case it belongs to the current stage.
            prev = "".join(buf).rstrip()[-1:] if buf else ""
            nxt = cmd[i + 1] if i + 1 < n else ""
            if prev == ">" or nxt == ">" or nxt.isdigit():
                buf.append(ch)
                i += 1
                stmt_start = False
                continue
            flush("&")
            i += 1
            stmt_start = True
            continue

        buf.append(ch)
        i += 1
        if not ch.isspace():
            stmt_start = False

    flush("")
    return stages


# ── Stage inspection ────────────────────────────────────────────────────────

def _scan_words(text: str, limit: int = 16) -> list[str]:
    """Return the leading whitespace-separated words of *text*.

    Quotes and escapes are honoured so ``a "b c"`` yields two words.  The
    returned words have their surrounding quotes removed but are otherwise
    verbatim — they are meant for inspection only, never to be re-quoted and
    re-joined (that would break globs such as ``*.py``).
    """
    words: list[str] = []
    i = 0
    n = len(text)
    while len(words) < limit:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        buf: list[str] = []
        while i < n and not text[i].isspace():
            ch = text[i]
            if ch == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if ch in ("'", '"'):
                q = ch
                i += 1
                while i < n and text[i] != q:
                    if q == '"' and text[i] == "\\" and i + 1 < n:
                        buf.append(text[i + 1])
                        i += 2
                        continue
                    buf.append(text[i])
                    i += 1
                i += 1
                continue
            buf.append(ch)
            i += 1
        words.append("".join(buf))
    return words


def _command_word(stage: str) -> str:
    """The command word of *stage*.

    Leading ``NAME=value`` assignments and redirections are skipped; a
    redirection's target word is consumed as well.  Returns ``""`` when the
    stage contains no command at all — for example a stage that is nothing
    but a redirection, which must never be wrapped in ``timeout``.
    """
    words = _scan_words(stage, limit=32)
    i = 0
    while i < len(words):
        word = words[i]
        i += 1
        if not word:
            continue
        if _ASSIGNMENT_RE.match(word):
            continue
        if _FD_DUP_RE.match(word) or _REDIRECT_ATTACHED_RE.match(word):
            continue
        if _REDIRECT_RE.match(word):
            i += 1  # the redirection target is the next word
            continue
        return word
    return ""


def _split_leading_assignments(stage: str) -> tuple[str, str]:
    """Split ``NAME=value ... rest`` into its two textual halves.

    The text is copied verbatim (never re-quoted) so that quoting, globs and
    variable expansion keep working exactly as the user wrote them.
    """
    words = _scan_words(stage, limit=32)
    if not words:
        return "", stage
    count = 0
    for word in words:
        if _ASSIGNMENT_RE.match(word):
            count += 1
        else:
            break
    if count == 0:
        return "", stage
    # Locate the end of the assignment prefix in the original text.
    pos = 0
    consumed = 0
    n = len(stage)
    while consumed < count and pos < n:
        while pos < n and stage[pos].isspace():
            pos += 1
        start = pos
        while pos < n and not stage[pos].isspace():
            pos += 1
        if pos == start:
            break
        consumed += 1
    prefix = stage[:pos].strip()
    remainder = stage[pos:].strip()
    if not remainder:
        return "", stage
    return prefix, remainder


# ── Timeout injection ───────────────────────────────────────────────────────

def timeout_binary() -> str | None:
    """Path to GNU ``timeout``, or ``None`` when it is not available."""
    if sys.platform == "win32":
        return None
    return shutil.which("timeout")


_KILL_AFTER_SUPPORT: dict[str, bool] = {}


def supports_kill_after(binary: str) -> bool:
    """True when *binary* understands GNU ``timeout --kill-after``.

    BusyBox's ``timeout`` does not, and passing the option to it would make
    every wrapped stage fail with "unrecognized option".  The probe result is
    cached, so this costs one subprocess per binary.
    """
    cached = _KILL_AFTER_SUPPORT.get(binary)
    if cached is not None:
        return cached
    ok = False
    try:
        res = subprocess.run(
            [binary, "--help"], capture_output=True, text=True, timeout=5
        )
        ok = "--kill-after" in ((res.stdout or "") + (res.stderr or ""))
    except Exception:
        ok = False
    _KILL_AFTER_SUPPORT[binary] = ok
    return ok


def wrap_stage(stage: str, timeout: int, binary: str = "timeout") -> str:
    """Wrap a single stage in a GNU ``timeout`` call.

    Shell builtins are returned unchanged: GNU ``timeout`` can only exec real
    binaries, and builtins such as ``cd`` must keep running in the parent
    shell so that they still affect the following stages.
    """
    if timeout <= 0:
        return stage

    word = _command_word(stage)
    if not word:
        return stage
    if os.path.basename(word) in SHELL_BUILTINS:
        return stage
    if word in SHELL_KEYWORDS:
        return stage
    if not _COMMAND_WORD_RE.match(word):
        # Not a plain program name (shell construct, redirection, ...).
        return stage

    prefix = f"{binary} --kill-after={int(KILL_GRACE_SECONDS)}s {int(timeout)}s" \
        if supports_kill_after(binary) else \
        f"{binary} {int(timeout)}s"

    assignments, remainder = _split_leading_assignments(stage)
    if assignments:
        # `timeout` cannot exec `NAME=value cmd`; `env` can.
        return f"{prefix} env {assignments} {remainder}"
    return f"{prefix} {stage}"


#: Body of the DEBUG trap used by :func:`_instrument_chain`.  It is emitted
#: inside single quotes, so ``$?`` and ``${PIPESTATUS[0]}`` are expanded when
#: the trap fires rather than when the trap is installed.
_TIMEOUT_TRAP_BODY = (
    "__orion_sr_p0=${PIPESTATUS[0]:-}; "
    "__orion_sr_prev=$?; "
    'if [ "$__orion_sr_prev" = 124 ] || [ "$__orion_sr_p0" = 124 ]; '
    "then __orion_sr_timed_out=1; fi"
)


def _instrument_chain(chain: str) -> str:
    """Wrap *chain* so that a stage timeout is always visible in the status.

    GNU ``timeout`` exits with 124 when it kills a stage, but the chain's exit
    status can hide that: ``sleep 300 || echo recovered`` and ``sleep 300 ; echo
    after`` both finish with status 0, so the timeout would silently go
    unreported.  A DEBUG trap observes the status of every command *before* the
    next one runs, which is exactly where the 124 of a masked stage is still
    visible.  The trap never alters the exit status of the command it precedes,
    so a genuine failure still reports its own code.

    For a pipeline the masked status is the *left* stage's, which the trap
    cannot see through ``$?`` (a pipeline reports its rightmost command).  The
    trap therefore reads ``${PIPESTATUS[0]}`` first — it still holds the
    pipeline's individual statuses at that point, and it has to be read before
    any other command of the trap body resets it.
    """
    return (
        "__orion_sr_timed_out=0\n"
        f"trap '{_TIMEOUT_TRAP_BODY}' DEBUG\n"
        f"{chain}"
        "\n__orion_sr_rc=$?\n"
        'if [ "$__orion_sr_timed_out" = 1 ]; then exit 124; fi\n'
        "exit $__orion_sr_rc"
    )


def build_timed_command(
    cmd: str,
    timeout: int,
    stages: list[tuple[str, str]] | None = None,
    binary: str | None = None,
) -> tuple[str, int, int]:
    """Rewrite *cmd* so that every external stage gets its own timeout.

    Returns ``(effective_command, stage_count, wrapped_count)``.
    """
    if stages is None:
        stages = split_chain(cmd)
    if not stages:
        return cmd, 0, 0

    if binary is None:
        binary = timeout_binary()

    # A command that defines a shell function cannot be split safely: the
    # function only exists in the shell that defined it, and wrapping the
    # call in `timeout` would turn it into "command not found".
    if any(_FUNCTION_DEF_RE.match(text) for text, _ in stages):
        return cmd, len(stages), 0

    parts: list[str] = []
    wrapped = 0
    for text, op in stages:
        if binary and timeout > 0:
            new_text = wrap_stage(text, timeout, binary)
        else:
            new_text = text
        if new_text != text:
            wrapped += 1
        parts.append(new_text)
        if op:
            parts.append(op)

    return " ".join(parts), len(stages), wrapped


# ── Process-tree termination ────────────────────────────────────────────────

def _session_pids(sid: int) -> list[int]:
    """Every live pid that belongs to session *sid*.

    Used to clean up children that moved into their own process group (GNU
    ``timeout`` does exactly that for the command it manages), which a plain
    ``killpg`` would miss.
    """
    pids: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/stat", "r") as fh:
                data = fh.read()
        except OSError:
            continue
        rparen = data.rfind(")")
        if rparen == -1:
            continue
        fields = data[rparen + 2:].split()
        if len(fields) < 4:
            continue
        try:
            psid = int(fields[3])
        except ValueError:
            continue
        if psid == sid:
            pids.append(pid)
    return pids


def kill_tree(pid: int) -> int:
    """Kill *pid* and every process descended from it.

    Three layers, because no single one is sufficient:

    1. the process **group** (the child is a session leader, so its pgid is
       its pid) — catches everything that stayed in the group;
    2. a ``/proc`` sweep of the whole **session** — catches children that were
       moved into their own process group, e.g. by GNU ``timeout``;
    3. the process itself, in case the group/session lookups failed.

    Returns the number of signals actually delivered.
    """
    if pid <= 0:
        return 0

    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
        return 0

    killed = 0
    my_pgid = os.getpgid(0)
    my_pid = os.getpid()

    # 1. process group
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = pid  # start_new_session=True => pgid == pid
    if pgid and pgid != my_pgid:
        try:
            os.killpg(pgid, signal.SIGKILL)
            killed += 1
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # 2. whole session (start_new_session=True => sid == pid)
    for victim in _session_pids(pid):
        if victim in (my_pid, pid):
            continue
        try:
            os.kill(victim, signal.SIGKILL)
            killed += 1
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # 3. the process itself
    try:
        os.kill(pid, signal.SIGKILL)
        killed += 1
    except (ProcessLookupError, PermissionError, OSError):
        pass

    return killed


# ── Execution ───────────────────────────────────────────────────────────────

@dataclass
class ShellResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    timed_out: bool = False
    #: True when one stage of the chain hit its own ``timeout`` budget.  This is
    #: detected even when the chain's exit status hides it (``a || b``,
    #: ``a ; b``, ``a | b`` all swallow the 124 of a timing-out stage).
    stage_timed_out: bool = False
    duration: float = 0.0
    stages: int = 1
    wrapped: int = 0
    command: str = ""
    killed: int = 0
    budget: float | None = None
    error: str = ""


def _pump(stream, sink: list) -> int:
    """Read *stream* into *sink* until EOF.  Never raises.

    Returns the number of bytes that were dropped because the capture limit
    was reached.  The stream is always drained to EOF, so a chatty command can
    never block on a full pipe.
    """
    total = 0
    dropped = 0
    try:
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                break
            total += len(chunk)
            if total <= MAX_OUTPUT_BYTES:
                sink.append(chunk)
            else:
                dropped += len(chunk)
    except Exception:
        pass
    return dropped


def run(
    cmd: str,
    timeout: int = DEFAULT_STAGE_TIMEOUT,
    cwd: str | None = None,
) -> ShellResult:
    """Run *cmd* with a per-stage timeout and guaranteed tree termination."""
    started = time.monotonic()

    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_STAGE_TIMEOUT

    stages = split_chain(cmd)
    effective, stage_count, wrapped = build_timed_command(cmd, timeout, stages)

    if wrapped:
        # At least one stage carries its own timeout, so make sure the timeout
        # is reported even when the chain's exit status would hide it.
        executable = _instrument_chain(effective)
    else:
        executable = effective

    if timeout > 0:
        budget = timeout * max(1, stage_count) + BUDGET_GRACE_SECONDS
        budget = min(budget, MAX_TOTAL_BUDGET)
    else:
        budget = None

    if sys.platform == "win32":
        argv = ["cmd.exe", "/c", executable]
        popen_kwargs: dict = {}
    else:
        argv = ["bash", "-c", executable]
        # start_new_session=True makes the child a session + group leader, so
        # the whole tree can be torn down without touching our own group.
        popen_kwargs = {"start_new_session": True}

    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )
    except Exception as exc:
        return ShellResult(
            returncode=-1,
            duration=time.monotonic() - started,
            stages=stage_count,
            wrapped=wrapped,
            command=effective,
            error=f"[ERROR] Failed to execute command: {exc}",
        )

    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    out_box: list[int] = []
    err_box: list[int] = []

    def _pump_out():
        out_box.append(_pump(proc.stdout, out_chunks))

    def _pump_err():
        err_box.append(_pump(proc.stderr, err_chunks))

    t_out = threading.Thread(target=_pump_out, daemon=True)
    t_err = threading.Thread(target=_pump_err, daemon=True)
    t_out.start()
    t_err.start()

    timed_out = False
    killed = 0
    try:
        proc.wait(timeout=budget)
    except subprocess.TimeoutExpired:
        timed_out = True
        killed = kill_tree(proc.pid)
        try:
            proc.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass

    # Drain whatever is left.  The readers are daemon threads, so a pipe that
    # is still held open by a stray process can never block the caller.
    t_out.join(timeout=READER_JOIN_GRACE)
    t_err.join(timeout=READER_JOIN_GRACE)
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream and not stream.closed:
                stream.close()
        except Exception:
            pass

    stdout = b"".join(out_chunks).decode("utf-8", errors="replace")
    stderr = b"".join(err_chunks).decode("utf-8", errors="replace")

    lost = (out_box[0] if out_box else 0) + (err_box[0] if err_box else 0)
    if lost:
        stdout += (
            f"\n... (output truncated, {lost} bytes exceeded the "
            f"{MAX_OUTPUT_BYTES} byte capture limit)"
        )

    return ShellResult(
        stdout=stdout,
        stderr=stderr,
        returncode=proc.returncode,
        timed_out=timed_out,
        stage_timed_out=(proc.returncode == TIMEOUT_EXIT_CODE),
        duration=time.monotonic() - started,
        stages=stage_count,
        wrapped=wrapped,
        command=effective,
        killed=killed,
        budget=budget,
    )


def describe(result: ShellResult, timeout: int) -> str:
    """Render a :class:`ShellResult` the way the ``shell`` tool reports it."""
    if result.error:
        return result.error

    output = result.stdout
    if result.stderr:
        output += f"\n[STDERR]\n{result.stderr}"

    if result.timed_out:
        budget = int(result.budget) if result.budget else int(result.duration)
        output += (
            f"\n[TIMEOUT] The command exceeded its overall budget of "
            f"{budget}s and the whole process tree was terminated."
        )
    elif result.stage_timed_out and timeout > 0:
        output += (
            f"\n[TIMEOUT] A command in the chain exceeded its {timeout}s limit "
            f"and was terminated (exit code {TIMEOUT_EXIT_CODE})."
        )

    if result.returncode not in (0, None):
        output += f"\n[EXIT CODE {result.returncode}]"

    return output.strip() or "[NO OUTPUT]"
