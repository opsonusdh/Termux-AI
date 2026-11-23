"""
permissions.py – Autonomy-first permission gate for Termux-AI and Windows-AI agent.

Philosophy
----------
The AI operates freely inside its project root. Permission is requested only when
an action would:

  1. Run a forbidden system command (sudo, reboot, …)
  2. Write into core/         – the AI's own source code
  3. Write into Termux-STT/   – the speech-to-text module
  4. Write/execute outside project root entirely

Everything else – workspace edits, package installs, reading files, running
scripts, piping data, even installing packages – proceeds without interruption.
"""

import os
import re
import sys
import json
import shlex
import subprocess
from functools import lru_cache
from typing import Iterable, List, Tuple

import paths
from core import shell_parse
from core import app_config
from core import confidential
from core import notifier
from core.renderer import (
    render_tool_code,
    render_code_snippet,
    language_for_path,
    RED,
    BOLD,
    RESET,
)

# Directory roots (dynamically resolved so it works on both Windows and Termux/Linux)
BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AI_ROOT   = BASE_DIR
CORE_DIR  = os.path.join(AI_ROOT, "core")
STT_DIR   = os.path.join(AI_ROOT, "Termux-STT")
WORKSPACE = os.path.join(AI_ROOT, "workspace")
TEMP_ROOT = os.path.join(WORKSPACE, ".tmp")

# Same file interface.py and tools.py read/write (paths.CONFIG_FILE) — this
# used to be recomputed independently here as BASE_DIR/config/config.json,
# which happened to match but would silently diverge (autonomous-mode checks
# reading a different file than the /autonomous command writes to, with no
# error) the moment paths.py's definition ever changed.
CONFIG_PATH = paths.CONFIG_FILE
# The canonical default set lives in core/app_config.py.  Three modules used
# to carry their own copy of it and they drifted: this one never learned
# about the "notify" flag, so a config.json it created was missing a key
# the interface expected.
DEFAULT_CONFIG = dict(app_config.DEFAULT_CONFIG)

# Keep the scratch area available.
os.makedirs(TEMP_ROOT, exist_ok=True)

if not os.path.exists(CONFIG_PATH):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=4)


#: Override flag for the autonomy setting.
#:
#: With ``autonomous: true`` in config.json the gate approves every command
#: except ``api.keys`` references — which makes the gate itself impossible to
#: exercise, and turns "the user asked for autonomy" into "no permission check
#: ever runs again".  Dropping this file into the scratch directory forces the
#: interactive gate back on without touching config.json, so the behaviour can
#: be observed (and tested) the way the user experiences it.
DISABLE_AUTONOMOUS_FILE = os.path.join(TEMP_ROOT, "disable_autonomous")


def autonomous_level() -> str:
    """The autonomy setting as one of ``"full"``, ``"semi"`` or ``"off"``.

    ``config.json`` stores the value under the ``autonomous`` key, which
    historically held a boolean.  ``true``/``false`` keep their original
    meaning (full/off) and the string ``"semi"`` selects the middle mode,
    so an old config file needs no migration.  The scratch-directory
    override flag wins over every config value.
    """
    if os.path.exists(DISABLE_AUTONOMOUS_FILE):
        return "off"
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return "off"
    value = cfg.get("autonomous", False)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("semi", "half", "ask"):
            return "semi"
        if word in ("true", "on", "yes", "1"):
            return "full"
        # Unknown words stay off — a typo in the config must not hand out
        # full autonomy silently; the gate asks instead.
        return "off"
    return "full" if value else "off"


def is_autonomous() -> bool:
    """Check if autonomous mode is enabled in config.json.

    True for both the full and the semi level — every routine check is
    bypassed either way.  The semi level only changes what happens when a
    credential shows up: the gate stops and asks the user instead of
    refusing outright.

    The scratch-directory override flag wins over the config setting.
    """
    return autonomous_level() != "off"


def is_autonomous_semi() -> bool:
    """True when autonomous mode runs at the ``semi`` level.

    Semi mode auto-approves the routine like full autonomy, but where the
    full level *refuses* a confidential file (nobody there to answer), the
    semi level *asks* the user who chose the mode — the human is the
    approval boundary.
    """
    return autonomous_level() == "semi"


def permission_snapshot() -> str:
    """One line describing the gate, for the model's prompt.

    The model used to learn what it was allowed to do by hitting the gate:
    every protected path, every command outside the workspace and every
    credential read cost a turn and produced a refusal.  One line in the
    prompt says the same thing for free — and because it is rebuilt for
    every request, a mode toggle takes effect on the next turn.
    """
    # Derived through is_autonomous()/is_autonomous_semi(), not from
    # autonomous_level() directly: tests (and any other override) patch
    # those two, and the snapshot must follow the patched gate.
    if is_autonomous():
        if is_autonomous_semi():
            return (
                "[PERMISSIONS] Autonomous semi mode is ON — routine shell "
                "commands and file writes run without asking, but anything "
                "that touches a confidential file, a credential or a secret "
                "stops and asks the user to approve it (the runtime waits "
                "for the answer)."
            )
        return (
            "[PERMISSIONS] Autonomous mode is ON — the permission gate is "
            "bypassed: shell commands and file writes run without asking. "
            "Confidential files (credentials, keys, tokens, personal memory) "
            "stay gated in every mode: they are refused, not read."
        )
    return (
        "[PERMISSIONS] Autonomous mode is OFF — the permission gate is "
        "active: shell commands outside the workspace, writes to core/ or "
        "Termux-STT/, confidential files (credentials, keys, tokens, personal "
        "memory) and other side effects ask the user first. manage_plugin is "
        "always available."
    )


def is_voice_available():
    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
    except Exception:
        config = DEFAULT_CONFIG

    if not config.get("tts_enabled", False):
        return False

    stt_path = os.path.expanduser(config["stt_path"])
    if stt_path not in sys.path:
        sys.path.append(stt_path)

    try:
        from main import listen  # noqa: F401

        check_cmd = "where edge-tts" if sys.platform == "win32" else "which edge-tts"
        if subprocess.run(
            check_cmd,
            shell=True,
            capture_output=True
        ).returncode != 0:
            return False

        return True

    except Exception:
        return False


# Directories the AI must never silently modify
PROTECTED_DIRS: List[str] = [CORE_DIR, STT_DIR]

# The real secrets files (config/api.keys and its Termux-STT twin).
# api.keys.template is the safe, distributable template and is NOT protected —
# only the genuine secrets file (basename exactly "api.keys") is gated.
API_KEYS_PATHS = [
    paths.API_KEYS_FILE,
    os.path.join(BASE_DIR, "Termux-STT", "api.keys"),
]

# Regex matching the token "api.keys" only as a *standalone* reference — never
# as part of "api.keys.template" or identifiers like "myapi.keys.bak".
#   (?<![\w.])  — not preceded by word-char or dot
#   api\.keys   — the literal filename
#   (?![\w.])   — not followed by word-char or dot
_API_KEYS_REF = re.compile(r"(?<![\w.])api\.keys(?![\w.])")

# Credential files belonging to the account plugins.  These hold OAuth client
# secrets and refresh tokens, so they are gated exactly like api.keys: a
# refresh token is a long-lived credential that grants mailbox, calendar and
# Drive access, and it must not be readable just because it does not happen to
# be named api.keys.
_GOOGLE_SECRET_NAMES = {"google_oauth.json", "google_pending_auth.json"}

_GOOGLE_SECRETS_REF = re.compile(
    r"(?<![\w.-])(?:google_oauth\.json|google_pending_auth\.json)(?![\w.-])"
)

#:\s The directory holding the per-service OAuth tokens.
_GOOGLE_TOKENS_DIRNAME = "google_tokens"

# Unconditionally blocked – these touch system state no AI should touch.
FORBIDDEN_COMMANDS = {
    "sudo", "su", "doas", "pkexec", "passwd",
    "shutdown", "reboot", "poweroff", "halt",
    "mount", "umount", "runas", "format",
}

# Pure read-only commands – they never create or change files on their own.
READ_ONLY_COMMANDS = {
    "cat", "ls", "pwd", "whoami", "id", "echo", "printf",
    "head", "tail", "grep", "find", "wc", "sort", "uniq",
    "awk", "sed", "tree", "which", "command", "stat", "du",
    "file", "basename", "dirname", "realpath", "date", "env",
    "printenv", "diff", "less", "more", "type", "ldd", "strings",
    # Windows native read-only commands:
    "dir", "findstr", "where", "get-childitem", "get-content",
    "get-location", "gci"
}

# Commands that modify the filesystem.
MUTATING_COMMANDS = {
    "rm", "rmdir", "mv", "cp", "mkdir", "touch", "dd", "wget",
    "truncate", "tee", "ln", "chmod", "chown",
    # Windows native file mutating commands:
    "del", "erase", "move", "copy", "xcopy", "robocopy", "md",
    "new-item", "remove-item", "copy-item", "move-item", "ni", "ri"
}

# Script/code interpreters – checked against what they are given to run.
INTERPRETER_COMMANDS = {
    "python", "python3", "bash", "sh", "zsh",
    "node", "nodejs", "perl", "ruby",
    # Windows native shell interpreters:
    "powershell", "powershell.exe", "cmd", "cmd.exe", "pwsh", "pwsh.exe"
}

# Package managers – installing software is a normal, routine AI task.
PACKAGE_MANAGERS = {
    "pip", "pip3", "npm", "pkg", "apt", "apt-get",
    "yarn", "npx", "gem", "cargo", "pipx",
    # Windows package managers:
    "winget", "choco", "scoop"
}

# Flags that accept a file output argument (wget -O, curl -o, etc.)
_OUTPUT_FLAGS = {"-o", "-O", "--output"}

# Device sinks and pseudo-files that are safe to write to.
_SPECIAL_SINKS = {
    os.path.realpath(os.devnull),
}
if sys.platform != "win32":
    _SPECIAL_SINKS.add(os.path.realpath("/dev/stdout"))
    _SPECIAL_SINKS.add(os.path.realpath("/dev/stderr"))

_SPECIAL_PREFIXES = (
    "/proc/self/fd/",
    "/dev/fd/",
)

# Regex patterns that flag dangerous inline (-c) code
_DANGEROUS_INLINE_PATTERNS = [
    re.compile(r'\brm\b[^;|&\n]*-[a-zA-Z]*r'),  # recursive rm
    re.compile(r':\s*\(\s*\)\s*\{'),            # fork bomb  :(){…}
    re.compile(r'\bdd\b.*\bof=/'),              # raw device overwrite
]


# Path utilities

def _commonpath_is_inside(base: str, path: str) -> bool:
    try:
        base = os.path.normpath(os.path.realpath(os.path.expanduser(base))).lower()
        path = os.path.normpath(os.path.realpath(os.path.expanduser(path))).lower()
        return os.path.commonpath([base, path]).lower() == base
    except Exception:
        return False


def _expand_path(path: str) -> str:
    # ``expandvars`` first: a path like ``$HOME/x`` must be resolved to the
    # real location it points at, otherwise it looks like a relative path
    # inside the project root and a destructive command against it would be
    # waved through.
    path = os.path.expandvars(path)
    return os.path.normpath(os.path.realpath(os.path.expanduser(path)))


def _is_protected(path: str) -> bool:
    """True when path is inside core/ or Termux-STT/."""
    p = _expand_path(path)
    return any(_commonpath_is_inside(d, p) for d in PROTECTED_DIRS)


def _is_outside_root(path: str) -> bool:
    """True when path escapes project root entirely."""
    return not _commonpath_is_inside(AI_ROOT, _expand_path(path))


def _is_special_sink(path: str) -> bool:
    """True for device sinks and pseudo-files that are not real files."""
    p = _expand_path(path)
    if p in _SPECIAL_SINKS:
        return True
    return any(p.startswith(prefix) for prefix in _SPECIAL_PREFIXES)


def _is_fd_dup_target(token: str) -> bool:
    """True for shell file-descriptor duplication syntax like &1 or 2."""
    t = token.strip()
    return bool(re.fullmatch(r'(?:&\d+|\d+)', t))


def _is_api_keys_file(path: str) -> bool:
    """True for the real ``api.keys`` secrets file.

    The companion ``api.keys.template`` (the safe, redistributable template) is
    explicitly excluded — only the genuine secrets file is gated.  Both
    ``config/api.keys`` and ``Termux-STT/api.keys`` are covered.
    """
    try:
        p = _expand_path(path)
    except Exception:
        return False
    name = os.path.basename(p)
    if name == "api.keys.template":
        return False
    return name == "api.keys"


def _is_api_keys_template(path: str) -> bool:
    """True for the safe ``api.keys.template`` file (NOT gated)."""
    try:
        p = _expand_path(path)
    except Exception:
        return False
    return os.path.basename(p) == "api.keys.template"


def _is_google_secret_file(path: str) -> bool:
    """True for Google OAuth credentials and stored refresh tokens.

    Covers ``config/google_oauth.json``, the pending-auth state file and every
    token under ``data/google_tokens/``.  These are long-lived credentials for
    the user's mailbox, calendar and Drive, so they are gated in every mode —
    the same rule that protects ``api.keys``.
    """
    try:
        p = _expand_path(path)
    except Exception:
        return False
    if os.path.basename(p) in _GOOGLE_SECRET_NAMES:
        return True
    return _commonpath_is_inside(
        os.path.join(AI_ROOT, "data", _GOOGLE_TOKENS_DIRNAME), p
    )


#: A token that looks like a file path rather than a word or a flag.
#: A token that looks like a file path rather than a word or a flag.
#: A single backslash is *not* a separator here — on POSIX a backslash
#: inside a token is far more often a regex escape (``grep \bword\b``)
#: than a Windows separator, and name-checking a regex as a "file" made
#: the gate refuse patterns that merely contained a secret-ish word.
#: Backslash paths are still recognised through the extension rule, which
#: is the shape every real Windows file argument has (``dir\file.py``).
_PATH_LIKE_RE = re.compile(r"^[^\s]*/[^\s]*$|^\S*\.[A-Za-z0-9]{1,8}$")


def _confidential_finding(path: str, content: bool = True) -> confidential.Finding:
    """The classifier's verdict on *path*.

    Relative paths are resolved against the project root, because that is
    the working directory every shell command runs in.
    """
    return confidential.classify(path, content=content, base=AI_ROOT)


def _looks_like_path(token: str) -> bool:
    """True when *token* names a file rather than a word or an option."""
    text = str(token or "").strip().strip("'\"")
    if not text or text.startswith("-"):
        return False
    return bool(_PATH_LIKE_RE.match(text))


def _confidential_references(command: str) -> List[Tuple[str, str]]:
    """Every path in *command* that points at a confidential file.

    Returns ``(token, reason)`` pairs.  The reason never carries the
    matched value — only a label saying what kind of secret it is.

    A path the command *writes* to is judged by its name alone: what was
    in the file before is not leaked by overwriting it, and a content check
    there would make every scratch-file write ask a question.
    """
    found: List[Tuple[str, str]] = []
    seen = set()
    for segment in _split_shell_chain(command):
        tokens = _safe_split(segment)
        if not tokens:
            continue
        written = set(
            _extract_write_targets(tokens) + _extract_output_flag_targets(tokens)
        )
        for token in tokens:
            if not _looks_like_path(token):
                continue
            if token in seen:
                continue
            seen.add(token)
            finding = _confidential_finding(token, content=token not in written)
            if finding.confidential:
                found.append((token, finding.reason))
    return found


def _segment_references_google_secret(segment: str) -> bool:
    """True if a command mentions a Google credential file by name."""
    if _GOOGLE_SECRETS_REF.search(segment or ""):
        return True
    # The token directory can also be referenced by its bare name.  A leading
    # '/' is fine (``data/google_tokens/``), a leading word character is not
    # (``mygoogle_tokens`` is a different file).
    return bool(re.search(r"(?<![\w.-])google_tokens(?![\w-])", segment or ""))


def _segment_references_api_keys(segment: str) -> bool:
    """True if a shell-command segment mentions ``api.keys`` anywhere — as a
    read target, write target, source argument, or inside inline (``-c``) code.

    Uses a word-boundary-style regex so that ``api.keys.template`` and
    identifiers like ``myapi.keys`` are *not* matched.
    """
    return bool(_API_KEYS_REF.search(segment))


def _segment_references_credential_constants(segment: str) -> bool:
    """True if a segment mentions paths.py credential constants.

    Catches ``paths.API_KEYS_FILE``, ``paths.GOOGLE_OAUTH_FILE``,
    ``paths.GOOGLE_TOKENS_DIR`` and bare ``API_KEYS_FILE`` etc. in inline code.
    """
    if not segment:
        return False
    return any(
        const in segment
        for const in (
            "API_KEYS_FILE",
            "GOOGLE_OAUTH_FILE",
            "GOOGLE_TOKENS_DIR",
        )
    )


# Known secret file patterns to detect in script bodies.
_SECRET_PATH_PATTERNS = (
    "api.keys",
    "google_oauth.json",
    "google_pending_auth.json",
    "google_tokens",
    "API_KEYS_FILE",
    "GOOGLE_OAUTH_FILE",
    "GOOGLE_TOKENS_DIR",
)


def _script_references_credential(script_path: str) -> bool:
    """True if *script_path* contains references to known credential files/constants.

    Reads up to 256 KB of the script file and checks for references to secret
    paths or path constants. This catches exfiltration scripts that don't
    contain credential *values* but *reference* the credential files.
    """
    # Resolve relative paths against the project root (same as _confidential_finding).
    expanded = os.path.expanduser(os.path.expandvars(str(script_path or "")))
    if not os.path.isabs(expanded):
        expanded = os.path.join(AI_ROOT, expanded)
    try:
        absolute = os.path.abspath(expanded)
    except (OSError, ValueError):
        return False

    try:
        size = os.path.getsize(absolute)
    except OSError:
        return False
    if size == 0 or size > 8 * 1024 * 1024:  # same limit as content scan
        return False

    try:
        with open(absolute, "rb") as handle:
            raw = handle.read(256 * 1024)
    except OSError:
        return False

    if b"\x00" in raw[:4096]:  # binary
        return False

    text = raw.decode("utf-8", errors="replace")
    return any(pattern in text for pattern in _SECRET_PATH_PATTERNS)


# ── finding the program a command runs ────────────────────────────────────────
#
# The gate cannot ask "is this command safe" — it can only ask "is this
# command about to run a file that reads your credentials".  The first
# version of that check looked at one shape of command: ``python script.py``.
# Everything else walked past it:
#
#     env python3 leak.py            (a wrapper in front)
#     python3 < leak.py              (the script on stdin)
#     cat leak.py | python3          (the script on a pipe)
#     python3 -c "exec(open('leak.py').read())"
#     bash -c 'python3 leak.py'      (the interpreter one level down)
#
# The rule that covers all of them: **a file is scanned when the command
# would run it as code** — the interpreter's own script argument, the file
# piped or redirected into an interpreter, or a path named inside inline
# code.  The first draft went further and scanned *every* file the command
# named, wherever it stood — which made ``grep pattern core/tools.py``
# refused as "a script that reads a credential file" whenever the chain
# happened to contain a ``python3 -c`` segment further along, because the
# browser tool's source legitimately mentions the credential-path constants
# it enforces.  A file a command merely *reads* (grep, sed, ls) is data, not
# code; only the stages that run code, and the stage whose output an
# interpreter will consume, are scanned.

#: A quoted string inside inline code — the shape a script path takes in
#: ``exec(open('leak.py').read())``.
_QUOTED_LITERAL_RE = re.compile(r"(['\"])(.*?)\1", re.S)


#: An interpreter named with a version suffix (``python3.11``) is still an
#: interpreter.
_VERSIONED_INTERPRETER_RE = re.compile(r"^python[0-9.]*$")


def _is_interpreter_word(word: str) -> bool:
    """True when *word* names a script interpreter."""
    name = os.path.basename(str(word or "")).lower()
    if name in INTERPRETER_COMMANDS:
        return True
    if _VERSIONED_INTERPRETER_RE.match(name):
        return True
    # The Windows Python launcher.
    return name == "py"


def _stage_runs_code(segment: str) -> bool:
    """True when this one simple command is an interpreter (behind wrappers).

    ``python3 script.py`` runs code; ``cat leak.py | python3`` splits into
    two stages and only the second one does — the first is what *feeds* it,
    which :func:`_command_script_paths` handles separately.
    """
    tokens = _safe_split(segment)
    if not tokens:
        return False
    wrapped = _wrapped_command(tokens)
    effective = wrapped if wrapped else tokens
    if not effective:
        return False
    return _is_interpreter_word(_command_word(effective))


def _inline_paths(inline: str) -> List[str]:
    """The file paths named inside a piece of inline code."""
    found: List[str] = []
    for token in _safe_split(inline):
        if _looks_like_path(token):
            found.append(token)
    for match in _QUOTED_LITERAL_RE.finditer(inline):
        candidate = match.group(2).strip()
        if candidate and _looks_like_path(candidate):
            found.append(candidate)
    return found


def _inline_runs_code(inline: str) -> bool:
    """True when a piece of inline code itself runs a program.

    ``bash -c 'python3 leak.py'`` never opens the file by name, but the
    shell inside it runs the interpreter that does.
    """
    return any(_stage_runs_code(stage) for stage in _split_shell_chain(inline))


def _interpreter_stage_paths(tokens: List[str]) -> List[str]:
    """The files one interpreter stage would run *as code*.

    The script file (the first positional argument), the ``<`` stdin
    redirect target, and any path named inside inline code (``-c "…"``).
    A module run (``python3 -m …``) names no script file — the module is
    the code, and its file arguments are the module's data; the dangerous
    ones are already gated by name wherever they appear.
    """
    paths: List[str] = []
    effective = _wrapped_command(tokens) or tokens
    index = 1
    while index < len(effective):
        tok = effective[index]
        if tok in {"-c", "-Command", "/C"}:
            if index + 1 < len(effective):
                inline = effective[index + 1]
                # A path inside inline code is code-to-run when the code
                # opens the file (exec(open('x').read())) or when the code
                # is itself a script that runs it (bash -c 'python3 x').
                # Merely naming a file — classify('x.py') — is not running
                # it, and scanning every named file made the gate refuse
                # probes of its own source.
                if "open(" in inline or _inline_runs_code(inline):
                    paths.extend(_inline_paths(inline))
            return paths
        if tok == "-m":
            return paths
        if tok == "<":
            if index + 1 < len(effective):
                paths.append(effective[index + 1])
            index += 2
            continue
        if tok.startswith("-"):
            index += 1
            continue
        paths.append(tok)  # the script file
        return paths
    return paths


def _command_script_paths(cmd: str) -> List[str]:
    """Every file the command could run *as code*, as paths.

    Scoped to the stages that matter, keeping every shape in the comment
    above covered:

    * an interpreter stage — its script file, its ``<`` redirect target and
      the paths its inline code opens or runs;
    * the stage immediately before an interpreter stage — whatever it reads
      may be exactly what the interpreter executes (``cat leak.py | python3``);
    * a wrapper's own arguments when the wrapped command is an interpreter
      (``xargs -a scripts.txt python3`` — the listed files are run).

    A file a non-interpreter stage merely reads — ``grep pattern file``,
    ``sed … file``, ``ls file`` — is data, not code, and is *not* returned:
    that over-collection made the gate refuse inspecting this project's own
    source whenever an unrelated ``python3 -c`` sat elsewhere in the chain.
    """
    stages = _split_shell_chain(cmd)
    runs = [_stage_runs_code(stage) for stage in stages]
    if not any(runs):
        return []

    found: List[str] = []
    seen = set()

    def collect(token: str) -> None:
        if not _looks_like_path(token) or token in seen:
            return
        seen.add(token)
        found.append(token)

    for index, segment in enumerate(stages):
        runs_code = runs[index]
        feeds_code = index + 1 < len(stages) and runs[index + 1]
        if not (runs_code or feeds_code):
            continue
        tokens = _safe_split(segment)
        if runs_code:
            for path in _interpreter_stage_paths(tokens):
                collect(path)
            wrapped = _wrapped_command(tokens)
            if wrapped is not None and len(wrapped) < len(tokens):
                # The wrapper's own arguments can name what the interpreter
                # will run: xargs -a scripts.txt python3.
                for token in tokens:
                    collect(token)
        if feeds_code:
            for token in tokens:
                collect(token)
    return found


def _script_credential_findings(cmd: str) -> List[Tuple[str, str, str]]:
    """The files the command would run whose body points at a credential.

    Returns ``(path, reason, script_content)`` triples.  The path is the one the
    command used; the reason never carries the content of the file; the
    script_content is the full text of the script (or empty string if it
    cannot be read).
    """
    script_paths = _command_script_paths(cmd)
    if not script_paths:
        return []
    findings: List[Tuple[str, str, str]] = []
    for path in script_paths:
        if _script_references_credential(path):
            # Read the full script content for display in the permission prompt.
            expanded = os.path.expanduser(os.path.expandvars(str(path or "")))
            if not os.path.isabs(expanded):
                expanded = os.path.join(AI_ROOT, expanded)
            script_content = ""
            try:
                absolute = os.path.abspath(expanded)
                size = os.path.getsize(absolute)
                if size > 0 and size <= 8 * 1024 * 1024:  # same limit as content scan
                    with open(absolute, "rb") as handle:
                        raw = handle.read()
                    if b"\x00" not in raw[:4096]:  # not binary
                        script_content = raw.decode("utf-8", errors="replace")
            except OSError:
                pass

            findings.append((
                path,
                f"'{path}' reads a credential file — running it would print "
                f"or transmit your keys",
                script_content,
            ))
    return findings


def _is_owned_path(path: str) -> bool:
    target = _expand_path(path)
    for owned in _load_owned_paths():
        if _commonpath_is_inside(owned, target):
            return True
    return False


def _path_verdict(path: str, content: bool = True) -> Tuple[bool, str]:
    """Whether *path* needs permission, and why.

    *content* decides whether the file's **contents** are inspected as well
    as its name.  A write target is judged by its name only: overwriting a
    scratch file that happens to hold an old key is not a secret leak, and
    a content check there would make ordinary scratch writes ask questions
    for no reason.  A read is judged by both.
    """
    if _is_special_sink(path):
        return False, "OK"

    # The api.keys secrets file is gated for reads and writes in EVERY mode.
    if _is_api_keys_file(path):
        rel = os.path.relpath(_expand_path(path), AI_ROOT)
        return True, (
            f"path '{rel}' is the api.keys secrets file; "
            f"explicit permission required (api.keys.template is allowed)"
        )

    # Google OAuth credentials and refresh tokens are gated the same way.
    if _is_google_secret_file(path):
        rel = os.path.relpath(_expand_path(path), AI_ROOT)
        return True, (
            f"path '{rel}' holds Google OAuth credentials; "
            f"explicit permission required in all modes"
        )

    # Any other confidential file — a credential the gate has never heard
    # of, judged by its name or by what is inside it.  Writing over one of
    # these is as destructive as reading one.
    finding = _confidential_finding(path, content=content)
    if finding.confidential:
        rel = os.path.relpath(_expand_path(path), AI_ROOT)
        return True, (
            f"path '{rel}' is confidential — {finding.reason}; "
            f"explicit permission required in all modes"
        )

    if _is_protected(path):
        rel = os.path.relpath(_expand_path(path), AI_ROOT)
        protected_name = next(
            os.path.basename(d) for d in PROTECTED_DIRS
            if _commonpath_is_inside(d, _expand_path(path))
        )
        return True, f"path '{rel}' is inside protected directory '{protected_name}/'"

    if _is_outside_root(path):
        return True, f"path '{path}' is outside project directory {AI_ROOT}"

    if _is_owned_path(path):
        return False, "OK"

    return False, "OK"


# Ownership registry --------------------------------------------------------

OWNERSHIP_STATE_PATH = os.path.join(TEMP_ROOT, "ownership.json")


def _load_owned_paths() -> set[str]:
    try:
        with open(OWNERSHIP_STATE_PATH, "r") as f:
            data = json.load(f)
        owned = data.get("owned_paths", [])
        if not isinstance(owned, list):
            return set()
        return {os.path.realpath(os.path.expanduser(p)) for p in owned if isinstance(p, str)}
    except Exception:
        return set()


def _save_owned_paths(paths: Iterable[str]) -> None:
    os.makedirs(os.path.dirname(OWNERSHIP_STATE_PATH), exist_ok=True)
    normalized = sorted({os.path.realpath(os.path.expanduser(p)) for p in paths})
    payload = {"owned_paths": normalized}
    tmp_path = f"{OWNERSHIP_STATE_PATH}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, OWNERSHIP_STATE_PATH)


# Shell parsing helpers -----------------------------------------------------

def _split_shell_chain(cmd: str) -> List[str]:
    """Split *cmd* into the simple commands it actually runs.

    The split is **structural**, not textual.  The previous implementation cut
    the command at every ``;``, ``&&``, ``||`` and newline, which tore compound
    blocks apart: in ``for i in 1 2 3; do rm -rf $HOME/x; done`` the cut landed
    right before ``rm``, so the segment's command word was ``do`` — a shell
    keyword — and the destructive command inside the loop was never inspected.

    ``core.shell_parse`` parses the command properly and returns the text of
    every simple command, including the ones nested in ``for``/``while``/
    ``until``/``if``/``case`` blocks, ``{ }`` groups, subshells and command
    substitutions.  Each of them is then checked on its own by
    :func:`_segment_needs_permission`.
    """
    return shell_parse.simple_commands(cmd)


def _safe_split(cmd: str) -> List[str]:
    try:
        return shlex.split(cmd, posix=(sys.platform != "win32"))
    except Exception:
        return []


#: A leading ``NAME=value`` assignment, e.g. ``FOO=bar rm -rf x``.
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: Commands whose real payload is *another* command.  The permission check has
#: to look through them, otherwise ``command rm -rf /tmp/x``,
#: ``env FOO=1 rm -rf /tmp/x``, ``xargs rm -rf`` or ``timeout 30 rm -rf x``
#: would slip past the gate.
_COMMAND_WRAPPERS = {
    "command", "builtin", "exec", "env", "nohup", "stdbuf", "setsid",
    "nice", "ionice", "time", "xargs", "timeout", "watch",
}

#: Options of the wrapper commands that take a separate value argument.
_WRAPPER_VALUE_OPTIONS = {
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "--class", "--classdata"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "watch": {"-n", "--interval"},
    "stdbuf": {"-i", "-o", "-e"},
}

#: ``timeout 30s`` / ``timeout 1.5m`` — the duration before the command.
#: Also accepts GNU ``timeout``'s colon forms: ``1:30`` (MM:SS) and
#: ``1:2:30`` (HH:MM:SS).
_DURATION_RE = re.compile(r"^\d+(\.\d+)?[smhd]?$|^\d{1,2}:\d{1,2}(:\d{1,2})?$")


def _command_word(tokens: List[str]) -> str:
    """The command word of *tokens*, ignoring leading ``NAME=value`` assignments.

    ``FOO=1 rm -rf x`` runs ``rm``; looking only at the first token would
    report ``FOO=1`` as the command and the ``rm`` would never be checked.
    """
    for tok in tokens:
        if not tok:
            continue
        if _ASSIGNMENT_RE.match(tok):
            continue
        return os.path.basename(tok).lower()
    return ""


def _cmd_name(segment: str) -> str:
    tokens = _safe_split(segment)
    return _command_word(tokens) if tokens else ""


def _wrapped_command(tokens: List[str]) -> List[str]:
    """Return the token list of the command a wrapper actually runs.

    ``None`` when *tokens* does not start with a wrapper, or when the wrapper
    has no command to run.
    """
    if len(tokens) < 2:
        return None

    cmd = _command_word(tokens)
    if cmd not in _COMMAND_WRAPPERS:
        return None

    # Find the wrapper itself (it may follow leading assignments).
    index = 0
    while index < len(tokens) and _ASSIGNMENT_RE.match(tokens[index]):
        index += 1
    if index >= len(tokens):
        return None
    if os.path.basename(tokens[index]).lower() != cmd:
        return None
    index += 1

    # Skip the wrapper's own options.
    value_opts = _WRAPPER_VALUE_OPTIONS.get(cmd, set())
    while index < len(tokens):
        tok = tokens[index]
        if tok == "--":
            index += 1
            break
        if tok.startswith("-"):
            index += 2 if tok in value_opts else 1
            continue
        break

    # `env` may carry its own assignments before the command.
    if cmd == "env":
        while index < len(tokens) and _ASSIGNMENT_RE.match(tokens[index]):
            index += 1

    # `timeout` takes a duration before the command.
    if cmd == "timeout" and index < len(tokens) and _DURATION_RE.match(tokens[index]):
        index += 1

    if index >= len(tokens):
        return None
    return tokens[index:]


def _extract_write_targets(tokens: List[str]) -> List[str]:
    targets: List[str] = []
    i = 0

    while i < len(tokens):
        tok = tokens[i]

        if tok in {">", ">>", "1>", "1>>", "2>", "2>>", "&>", "&>>"}:
            if i + 1 < len(tokens):
                nxt = tokens[i + 1]
                if not _is_fd_dup_target(nxt):
                    targets.append(nxt)
                i += 2
                continue

        m = re.match(r"^(?:(?P<fd>\d+)?(?P<op>>>|>>|>|<|<<)|(?P<amp>&>>|&>))(?P<target>.+)$", tok)
        if m:
            tgt = m.group("target").strip()
            if tgt and not _is_fd_dup_target(tgt):
                targets.append(tgt)

        i += 1

    return targets


def _extract_output_flag_targets(tokens: List[str]) -> List[str]:
    targets: List[str] = []
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in _OUTPUT_FLAGS and i + 1 < len(tokens):
            targets.append(tokens[i + 1])
            i += 2
            continue
        if tok.startswith("--output="):
            targets.append(tok.split("=", 1)[1])
        i += 1
    return targets


def _non_flag_args(tokens: List[str]) -> List[str]:
    return [t for t in tokens[1:] if t and not t.startswith("-")]


def _target_directory_arg(tokens: List[str]) -> str | None:
    for i, tok in enumerate(tokens):
        if tok in {"-t", "--target-directory"} and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith("--target-directory="):
            return tok.split("=", 1)[1]
    return None


# Per-category permission checks -------------------------------------------

def _check_write_targets(cmd: str, targets: List[str]) -> Tuple[bool, str]:
    for t in targets:
        # A write target is judged by its name, not by what is inside it:
        # overwriting a file is not a way to leak its contents.
        needs, reason = _path_verdict(t, content=False)
        if needs:
            return True, f"'{cmd}': {reason}"
    return False, "OK"


def _check_mutating(cmd: str, tokens: List[str]) -> Tuple[bool, str]:
    positional = _non_flag_args(tokens)
    targets: List[str] = []

    if cmd in {"rm", "rmdir", "mkdir", "touch", "truncate", "del", "erase", "remove-item", "ri", "new-item", "ni"}:
        targets = positional

    elif cmd == "tee":
        targets = positional

    elif cmd == "dd":
        # dd commonly uses of=PATH rather than a positional output argument.
        targets = [
            tok.split("=", 1)[1]
            for tok in tokens[1:]
            if tok.startswith("of=") and tok.split("=", 1)[1]
        ]
        if not targets:
            return True, "'dd' can write to an arbitrary target and needs permission"

    elif cmd == "wget":
        # wget normally writes downloaded content to disk. Only explicit
        # stdout mode is treated as non-mutating.
        stdout_mode = any(tok in {"-O-", "--output-document=-"} for tok in tokens[1:])
        if "-O" in tokens[1:]:
            idx = tokens.index("-O")
            stdout_mode = idx + 1 < len(tokens) and tokens[idx + 1] == "-"
        if not stdout_mode:
            return True, "'wget' normally writes downloaded content to the filesystem"

    elif cmd in {"ln", "copy-item", "move-item", "cp", "mv"}:
        dest_dir = _target_directory_arg(tokens)
        if dest_dir:
            targets = [dest_dir] + positional
        else:
            targets = positional[-1:] if positional else []

    elif cmd in {"copy", "move", "xcopy", "robocopy"}:
        targets = positional[-1:] if len(positional) > 1 else positional

    elif cmd in {"chmod", "chown", "icacls"}:
        targets = positional[1:] if len(positional) > 1 else []

    targets += _extract_write_targets(tokens)

    if cmd in {"rm", "rmdir", "del", "erase", "remove-item", "ri"} and not targets:
        return True, f"'{cmd}' with no verifiable target path"

    return _check_write_targets(cmd, targets)


def _extract_script_path(tokens: List[str], interpreter: str) -> str | None:
    """Return the script file path being executed, or None.

    Handles:
    - ``python script.py`` → ``script.py``
    - ``python -c "code"`` → None (inline, handled separately)
    - ``python -m module`` → None (module, no file)
    - ``bash script.sh`` → ``script.sh``
    - ``node script.js`` → ``script.js``
    - ``perl script.pl`` → ``script.pl``
    """
    i = 1
    while i < len(tokens):
        tok = tokens[i]

        if tok == "-m":
            return None  # module, no file

        if tok in {"-c", "-Command", "/C"}:
            return None  # inline code, handled separately

        # Skip flag arguments.
        if tok.startswith("-") or (sys.platform == "win32" and tok.startswith("/")):
            i += 1
            continue

        # This is the script file.
        return tok

    return None


def _check_interpreter(cmd: str, tokens: List[str]) -> Tuple[bool, str]:
    # Check inline code first (existing logic).
    i = 1
    while i < len(tokens):
        tok = tokens[i]

        if tok == "-m":
            return False, "OK"

        if tok in {"-c", "-Command", "/C"} and i + 1 < len(tokens):
            inline = tokens[i + 1]

            # api.keys references in inline code are always gated.
            if _segment_references_api_keys(inline):
                return True, "inline code references the api.keys secrets file"

            # So are the Google OAuth credentials and refresh tokens.
            if _segment_references_google_secret(inline):
                return True, "inline code references a Google OAuth credential file"

            # Also gate references to the paths.py constants for secret files.
            if _segment_references_credential_constants(inline):
                return True, "inline code references a credential path constant"

            for pdir in PROTECTED_DIRS:
                name = os.path.basename(pdir)
                if pdir in inline or name in inline:
                    return True, f"inline code references protected directory '{name}/'"

            for pat in _DANGEROUS_INLINE_PATTERNS:
                if pat.search(inline):
                    return True, "inline code contains a potentially destructive pattern"

            # The inline code is a shell script in its own right, so the
            # commands it actually runs have to be checked too — otherwise
            # `bash -c "rm /tmp/x"` would pass just because the pattern list
            # above only looks for the recursive form.
            for sub in shell_parse.simple_commands(inline):
                needs, reason = _segment_needs_permission(sub)
                if needs:
                    return True, f"inline code: {reason}"

            return False, "OK"

        # Skip flag arguments. On Windows, flags can start with '/'.
        # On POSIX, '/' starts absolute paths, so only skip '-' there.
        if tok.startswith("-") or (sys.platform == "win32" and tok.startswith("/")):
            i += 1
            continue

        # Positional argument — could be a script file.
        script_path = tok
        needs, reason = _path_verdict(script_path)
        if needs:
            return True, f"'{cmd}': {reason}"

        # The script's *body* is not checked here.  That check lives in
        # :func:`_script_credential_findings`, which looks at the whole
        # command instead of one segment — so it also catches the forms this
        # per-segment check never saw (``env python f.py``, ``python < f.py``,
        # ``cat f.py | python``, ``python -c "exec(open('f.py').read())"``)
        # and, unlike this one, it runs in autonomous mode too.

        return False, "OK"

    return False, "OK"


def _check_generic(cmd: str, tokens: List[str]) -> Tuple[bool, str]:
    targets = _extract_write_targets(tokens) + _extract_output_flag_targets(tokens)
    return _check_write_targets(cmd, targets)


# Public API ---------------------------------------------------------------

def _segment_needs_permission(segment: str) -> Tuple[bool, str]:
    tokens = _safe_split(segment)
    if not tokens:
        return False, "OK"
    return _tokens_needs_permission(tokens, segment)


def _tokens_needs_permission(tokens: List[str], raw: str) -> Tuple[bool, str]:
    """Decide whether one simple command needs explicit permission.

    *tokens* is the token list of the command and *raw* its original text (the
    api.keys reference check works on the raw text, because it has to see the
    exact filename).
    """
    cmd = _command_word(tokens)

    # api.keys (the real secrets file, never the .template) is ALWAYS gated —
    # for both reads and writes, in every mode.  Checking the whole segment
    # catches it whether it appears as a read target, write target, source
    # argument, or inside inline (-c) code.
    if _segment_references_api_keys(raw):
        return True, (
            "command references the api.keys secrets file; "
            "explicit permission required in all modes"
        )

    # ... and the Google credential files are in the same class.
    if _segment_references_google_secret(raw):
        return True, (
            "command references a Google OAuth credential file; "
            "explicit permission required in all modes"
        )

    # A directory change alters the meaning of every following relative path
    # in the shell chain. Require explicit approval instead of pretending we
    # can safely infer arbitrary shell working-directory state here.
    if cmd in {"cd", "pushd", "popd"}:
        # Allow cd to project root or inside it without permission in non-autonomous mode.
        # The target is the first non-flag argument (or empty for "cd" = home).
        target = None
        for tok in tokens[1:]:
            if tok.startswith("-"):
                continue
            target = tok
            break
        if target is not None:
            expanded = _expand_path(target)
            if _commonpath_is_inside(AI_ROOT, expanded):
                return False, "OK"
        # For "cd" with no args (goes to HOME) or target outside project root, ask.
        return True, f"working-directory change via '{cmd}' requires explicit permission"

    if cmd in FORBIDDEN_COMMANDS:
        return True, f"'{cmd}' is a forbidden system command"

    # A wrapper (``env``, ``command``, ``nohup``, ``xargs``, ``timeout`` …)
    # runs another command.  The check has to look at *that* command, or
    # ``command rm -rf /tmp/x`` would be waved through as a read-only
    # ``command``.
    wrapped = _wrapped_command(tokens)
    if wrapped is not None and len(wrapped) < len(tokens):
        return _tokens_needs_permission(wrapped, raw)

    if cmd in READ_ONLY_COMMANDS:
        write_targets = _extract_write_targets(tokens) + _extract_output_flag_targets(tokens)
        if write_targets:
            needs, reason = _check_write_targets(cmd, write_targets)
            if needs:
                return True, reason
        # A read-only command is still a read.  ``cat ~/.ssh/id_rsa`` and
        # ``head notes.txt`` must not walk past the gate just because the
        # command itself never changes anything.
        for token in tokens[1:]:
            if not _looks_like_path(token):
                continue
            if token in write_targets:
                # The path is written to, not read from: what was in it
                # before is not leaked by overwriting it.
                continue
            finding = _confidential_finding(token)
            if finding.confidential:
                return True, (
                    f"'{cmd}' reads '{token}', which is confidential — "
                    f"{finding.reason}"
                )
        return False, "OK"

    if cmd in PACKAGE_MANAGERS:
        targets = _extract_write_targets(tokens) + _extract_output_flag_targets(tokens)
        if targets:
            return _check_write_targets(cmd, targets)
        return False, "OK"

    if cmd in MUTATING_COMMANDS:
        needs, reason = _check_mutating(cmd, tokens)
        if needs:
            return True, reason
        redir_targets = _extract_write_targets(tokens) + _extract_output_flag_targets(tokens)
        if redir_targets:
            return _check_write_targets(cmd, redir_targets)
        return False, "OK"

    if cmd in INTERPRETER_COMMANDS:
        needs, reason = _check_interpreter(cmd, tokens)
        if needs:
            return True, reason
        redir_targets = _extract_write_targets(tokens) + _extract_output_flag_targets(tokens)
        if redir_targets:
            return _check_write_targets(cmd, redir_targets)
        return False, "OK"

    return _check_generic(cmd, tokens)


def command_needs_permission(cmd: str) -> bool:
    """Return True if any segment of *cmd* requires explicit permission.

    In autonomous mode everything is auto-approved **except** references to
    confidential files — the api.keys secrets file, the Google credential
    files, and any other file the classifier recognises as holding a
    credential.  Those always require explicit permission regardless of
    mode.
    """
    # The api.keys reference is looked for in the *whole* command, not in the
    # individual segments: it can sit somewhere that is not a command of its
    # own — a ``for`` word list, a loop header, an assignment — while the
    # command that finally reads the file only mentions a variable.
    if (_segment_references_api_keys(cmd)
            or _segment_references_google_secret(cmd)
            or _segment_references_credential_constants(cmd)):
        return True

    # A confidential file named in the command — by path or by name.
    if _confidential_references(cmd):
        return True

    # A file the command would *run* whose body points at a credential.
    if _script_credential_findings(cmd):
        return True

    if is_autonomous():
        # Credential files are the one thing that stays gated in autonomous mode.
        return False

    segments = _split_shell_chain(cmd)

    return any(
        _segment_needs_permission(seg)[0]
        for seg in segments
    )


def _ask_permission(requests: List[Tuple[str, str]]) -> Tuple[bool, str]:
    """Show what the AI wants to do and wait for the user's answer.

    *requests* is a list of ``(what, why)`` pairs.  The answer applies to all
    of them, exactly like the shell gate it was extracted from.
    """
    print("\n[PERMISSION] The AI wants to run:")
    for segment, reason in requests:
        # The command is highlighted as bash, so what is about to run reads
        # like the code it is — not like a wall of plain text.  It is *not*
        # drawn in the markdown renderer's gutter box: that frame belongs to
        # a fenced code block, and here it would only put a box around every
        # single line the gate asks about.
        snippet = render_tool_code(segment, "bash")
        if snippet:
            for line in snippet:
                print(f"  {line}")
        else:
            print(f"  {segment}")
        print(f"  Reason: {reason}")

    try:
        if is_voice_available():
            reasons = "; ".join(reason for _, reason in requests)
            text_alert = f"Permission required. {reasons}"
            subprocess.Popen(
                (
                    'edge-tts '
                    '--voice "en-US-AndrewNeural" '
                    f'--text "{text_alert}" '
                    '--write-media - | mpv -'
                ),
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=(sys.platform != "win32"),
            )
    except Exception:
        pass

    # The runtime is about to block on the user's answer.  If the terminal is
    # in the background — the normal state on a phone — nothing on screen
    # says that the AI is waiting, so push a notification.  With "notify"
    # switched off this is a no-op.
    notifier.permission(requests)

    while True:
        try:
            inp = input("  Allow all listed actions? [y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("  Permission request aborted; command was not executed.")
            return False, "Permission request aborted"

        if inp in {"y", "yes"}:
            return True, "OK"
        if inp in {"n", "no"}:
            return False, "Denied: explicit user denial"

        print("  Please answer 'y' or 'n'.")


def _credential_requests(
    cmd: str,
    references_api_keys: bool,
    references_google_secrets: bool,
    references_secret_constants: bool,
    confidential: list,
    script_findings: list,
) -> List[Tuple[str, str]]:
    """The ``(what, why)`` pairs for everything credential-shaped in *cmd*.

    Shared by the interactive gate and semi-autonomous mode, so a semi
    approval asks about exactly what the interactive gate would have asked
    about — no more, no less.
    """
    requests: List[Tuple[str, str]] = []

    if references_api_keys:
        requests.append((cmd, (
            f"command references the api.keys secrets file; "
            "explicit permission required in all modes"
        )))
    if references_google_secrets and not references_api_keys:
        requests.append((cmd, (
            "command references a Google OAuth credential file; "
            "explicit permission required in all modes"
        )))
    if references_secret_constants and not references_api_keys:
        requests.append((cmd, (
            "inline code references a credential path constant; "
            "explicit permission required in all modes"
        )))

    for token, reason in confidential:
        requests.append((token, f"'{token}' is confidential — {reason}"))

    # A script that reads a credential file is the first half of the attack
    # the user described: write a file that prints the keys, run it, and the
    # keys are on the screen and in the log.  The file itself is harmless
    # until it runs, so the question belongs here.
    for path, reason, script_content in script_findings:
        # Build a detailed reason with the full script content and a warning.
        detailed_reason = (
            f"{RED}{BOLD}! WARNING: This script could potentially reveal your credentials{RESET}\n"
            f"{reason}\n"
        )
        if script_content:
            # Include the full script content, syntax-highlighted.
            lang = language_for_path(path)
            highlighted = render_code_snippet(script_content, lang)
            if highlighted:
                detailed_reason += "\n" + "\n".join(highlighted)
        requests.append((f"run {path}", detailed_reason))

    return requests


def validate_command(cmd: str) -> Tuple[bool, str]:
    """Validate a complete shell command and obtain explicit approval when needed.

    In autonomous mode everything is auto-approved **except** references to
    confidential files.  The full level refuses them outright — nobody at
    the keyboard to answer a question about a secret — while the semi level
    stops and asks the user, exactly like the interactive gate.
    """
    segments = _split_shell_chain(cmd)

    # The api.keys secrets file is gated in every mode.  The reference is
    # looked for in the *whole* command, because it can sit somewhere that is
    # not a command of its own — a ``for`` word list, a loop header, an
    # assignment — while the command that finally reads the file only
    # mentions a variable.
    references_api_keys = _segment_references_api_keys(cmd)
    references_google_secrets = _segment_references_google_secret(cmd)
    references_secret_constants = _segment_references_credential_constants(cmd)
    references_secrets = references_api_keys or references_google_secrets or references_secret_constants

    # Every path in the command that points at a confidential file.
    confidential = _confidential_references(cmd)

    # Every file the command would *run* whose body points at a credential.
    # This is computed before the mode branch on purpose: the script scan
    # used to live inside the per-segment check, which the autonomous branch
    # returns before it ever reaches — so in autonomous mode
    # ``python3 leak.py`` ran unchallenged and printed the keys.
    script_findings = _script_credential_findings(cmd)

    if is_autonomous():
        if not references_secrets and not confidential and not script_findings:
            return True, "Autonomous mode active (permission gate bypassed)"

        if is_autonomous_semi():
            # Semi-autonomous: the routine is still auto-approved, but a
            # credential is neither waved through nor refused — the gate
            # stops and puts the question to the user, exactly like the
            # interactive gate does.  Semi mode is chosen in an interactive
            # session, so the ask is the whole point.
            requests = _credential_requests(
                cmd, references_api_keys, references_google_secrets,
                references_secret_constants, confidential, script_findings,
            )
            if not requests:
                return True, "OK"
            return _ask_permission(requests)

        # Refuse rather than ask.  Asking in autonomous mode would either
        # block a background worker on stdin or silently consume the user's
        # keystrokes; refusing is the same answer without the trap.
        detail = "; ".join(
            f"{token} ({reason})" for token, reason in confidential
        )
        if script_findings:
            script_detail = "; ".join(
                f"{path} (reads a credential file)" for path, _, _ in script_findings
            )
            detail = (f"{detail}; " if detail else "") + script_detail
        if not detail:
            detail = "the command references a credential file"
        return False, (
            f"blocked in autonomous mode — {detail}. Confidential files stay "
            f"gated in every mode; switch the permission gate back on to "
            f"approve this by hand."
        )

    requests = _credential_requests(
        cmd, references_api_keys, references_google_secrets,
        references_secret_constants, confidential, script_findings,
    )

    for segment in segments:
        if references_secrets and (
            _segment_references_api_keys(segment)
            or _segment_references_google_secret(segment)
            or _segment_references_credential_constants(segment)
        ):
            # Already reported above, as the whole command.
            continue
        needs, reason = _segment_needs_permission(segment)
        if needs:
            requests.append((segment, reason))

    if not requests:
        return True, "OK"

    return _ask_permission(requests)


#: Extensions whose files are *run*: a file with one of these is a program,
#: so code in it that points at a credential store is worth a question.
#: Prose is not — a manual that mentions ``config/api.keys`` is documentation,
#: not an exfiltration script.
_CODE_EXTENSIONS = frozenset({
    ".py", ".pyw", ".pyi", ".sh", ".bash", ".zsh", ".ksh", ".fish",
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".rb", ".pl", ".php",
    ".ps1", ".psm1", ".bat", ".cmd", ".vbs", ".lua", ".r", ".jl", ".swift",
    ".go", ".rs", ".c", ".h", ".cpp", ".java", ".cs", ".sql", ".awk",
})


def _is_code_file(path: str) -> bool:
    """True when *path* names a file that could be run as a program."""
    extension = os.path.splitext(str(path or ""))[1].lower()
    return extension in _CODE_EXTENSIONS


def request_content_permission(operation: str, path: str,
                               content: str) -> Tuple[bool, str]:
    """Ask before writing content that points at a credential store.

    This closes the *first* half of the chain the user described: writing a
    program that reads ``config/api.keys``.  The file itself is harmless — a
    text file that names a path leaks nothing — but it is the thing the next
    step executes, so the gate asks while the intent is still visible.

    Only code files are asked about.  Documentation that mentions the path is
    not a threat, and gating it would make an ordinary edit noisy.

    In full-autonomous mode there is nobody to answer, so the write is
    refused — the same answer the shell gate gives for a credential
    reference.  Semi-autonomous mode stops and asks instead.
    """
    if not content or not confidential.references_credential_path(content):
        return True, "OK"
    if not _is_code_file(path):
        return True, "OK"

    p = _expand_path(path)
    rel = os.path.relpath(p, AI_ROOT)
    detail = ("the content points at a credential store "
              "(config/api.keys or the Google OAuth token files)")

    if is_autonomous() and not is_autonomous_semi():
        return False, (
            f"[PERMISSION DENIED] '{rel}' would contain code that reads a "
            f"credential — {detail}. Refused in autonomous mode; switch the "
            f"permission gate back on to write it."
        )

    approved, reason = _ask_permission([(f"{operation} {rel}", detail)])
    if not approved:
        return False, f"[PERMISSION DENIED] '{rel}': {reason}"
    return True, "OK"


def request_file_permission(operation: str, path: str) -> Tuple[bool, str]:
    """Request permission for a direct file-system operation (read/write/find_replace).

    Used by ``tools.py`` for file operations that bypass the shell.  Returns
    ``(approved, reason)`` — when *approved* is ``True`` the caller may proceed.

    A confidential file — a credential, a token, personal memory — is asked
    about directly, whatever the mode: the user is told what the file is and
    why it is sensitive, and decides.  In full-autonomous mode there is
    nobody to answer, so the read is refused; semi-autonomous mode asks.

    All other paths defer to the existing ``validate_command`` logic so that
    nothing is *automatically* rejected — the user is always asked first.
    """
    p = _expand_path(path)
    rel = os.path.relpath(p, AI_ROOT)

    # A confidential file is a question about a secret.  It is asked here,
    # directly, because the shell-shaped command below cannot say *why* the
    # file is sensitive.  The *contents* only matter for a read: writing
    # over a file does not leak what was in it.
    finding = _confidential_finding(path, content=(operation == "read"))
    if finding.confidential:
        if is_autonomous() and not is_autonomous_semi():
            return False, (
                f"[PERMISSION DENIED] '{rel}' is confidential — "
                f"{finding.reason}. Confidential files stay gated in every "
                f"mode; switch the permission gate back on to read it."
            )
        approved, reason = _ask_permission([(
            f"{operation} {rel}",
            f"'{rel}' is confidential — {finding.reason}",
        )])
        if not approved:
            return False, f"[PERMISSION DENIED] '{rel}': {reason}"
        return True, "OK"

    # Build a representative shell command so the existing permission logic
    # (which knows about api.keys, protected dirs, outside-root, etc.) is
    # reused rather than duplicated.
    if operation == "read":
        cmd = f"cat {shlex.quote(path)}"
    elif operation == "write":
        cmd = f"cat > {shlex.quote(path)}"
    elif operation == "find_replace":
        cmd = f"sed -i s/x/y/ {shlex.quote(path)}"
    else:
        cmd = f"cat {shlex.quote(path)}"

    approved, reason = validate_command(cmd)
    if not approved:
        return False, f"[PERMISSION DENIED] '{rel}': {reason}"
    return True, "OK"
