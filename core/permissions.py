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
DEFAULT_CONFIG = {
    "stt_path": os.path.join(BASE_DIR, "Termux-STT"),
    "tts_enabled": False,
    "show_details": False,
    "autonomous": False,
}

# Keep the scratch area available.
os.makedirs(TEMP_ROOT, exist_ok=True)

if not os.path.exists(CONFIG_PATH):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=4)


def is_autonomous() -> bool:
    """Check if autonomous mode is enabled in config.json."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return bool(cfg.get("autonomous", False))
    except Exception:
        return False


def set_autonomous(enabled: bool) -> bool:
    """Enable or disable autonomous mode in config.json."""
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        data = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        data["autonomous"] = bool(enabled)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception:
        return False


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


def is_inside_root(path: str) -> bool:
    return _commonpath_is_inside(AI_ROOT, path)


def is_inside_core(path: str) -> bool:
    return _commonpath_is_inside(CORE_DIR, path)


def is_inside_workspace(path: str) -> bool:
    return _commonpath_is_inside(WORKSPACE, path)


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


def _segment_references_api_keys(segment: str) -> bool:
    """True if a shell-command segment mentions ``api.keys`` anywhere — as a
    read target, write target, source argument, or inside inline (``-c``) code.

    Uses a word-boundary-style regex so that ``api.keys.template`` and
    identifiers like ``myapi.keys`` are *not* matched.
    """
    return bool(_API_KEYS_REF.search(segment))


def _is_owned_path(path: str) -> bool:
    target = _expand_path(path)
    for owned in _load_owned_paths():
        if _commonpath_is_inside(owned, target):
            return True
    return False


def _path_verdict(path: str) -> Tuple[bool, str]:
    if _is_special_sink(path):
        return False, "OK"

    # The api.keys secrets file is gated for reads and writes in EVERY mode.
    if _is_api_keys_file(path):
        rel = os.path.relpath(_expand_path(path), AI_ROOT)
        return True, (
            f"path '{rel}' is the api.keys secrets file; "
            f"explicit permission required (api.keys.template is allowed)"
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


def register_owned_path(path: str) -> None:
    target = _expand_path(path)
    owned = _load_owned_paths()
    owned.add(target)
    _save_owned_paths(owned)


def unregister_owned_path(path: str) -> None:
    target = _expand_path(path)
    owned = _load_owned_paths()
    owned.discard(target)
    _save_owned_paths(owned)


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
        needs, reason = _path_verdict(t)
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


def _check_interpreter(cmd: str, tokens: List[str]) -> Tuple[bool, str]:
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

        if tok.startswith("-") or tok.startswith("/"):
            i += 1
            continue

        needs, reason = _path_verdict(tok)
        if needs:
            return True, f"'{cmd}': {reason}"
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

    # A directory change alters the meaning of every following relative path
    # in the shell chain. Require explicit approval instead of pretending we
    # can safely infer arbitrary shell working-directory state here.
    if cmd in {"cd", "pushd", "popd"}:
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
            return _check_write_targets(cmd, write_targets)
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

    In autonomous mode everything is auto-approved **except** references to the
    api.keys secrets file, which always requires explicit permission regardless
    of mode.
    """
    # The api.keys reference is looked for in the *whole* command, not in the
    # individual segments: it can sit somewhere that is not a command of its
    # own — a ``for`` word list, a loop header, an assignment — while the
    # command that finally reads the file only mentions a variable.
    if _segment_references_api_keys(cmd):
        return True

    if is_autonomous():
        # api.keys is the one thing that stays gated in autonomous mode.
        return False

    segments = _split_shell_chain(cmd)

    return any(
        _segment_needs_permission(seg)[0]
        for seg in segments
    )


def validate_command(cmd: str) -> Tuple[bool, str]:
    """Validate a complete shell command and obtain explicit approval when needed.

    In autonomous mode everything is auto-approved **except** references to the
    api.keys secrets file, which always requires explicit permission regardless
    of mode.
    """
    segments = _split_shell_chain(cmd)

    # The api.keys secrets file is gated in every mode.  The reference is
    # looked for in the *whole* command, because it can sit somewhere that is
    # not a command of its own — a ``for`` word list, a loop header, an
    # assignment — while the command that finally reads the file only
    # mentions a variable.
    references_api_keys = _segment_references_api_keys(cmd)

    # In autonomous mode, auto-approve all segments EXCEPT those that
    # reference the api.keys secrets file.  api.keys is gated in every mode.
    if is_autonomous() and not references_api_keys:
        return True, "Autonomous mode active (permission gate bypassed)"

    requests: List[Tuple[str, str]] = []

    if references_api_keys:
        requests.append((cmd, (
            "command references the api.keys secrets file; "
            "explicit permission required in all modes"
        )))

    for segment in segments:
        if references_api_keys and _segment_references_api_keys(segment):
            # Already reported above, as the whole command.
            continue
        needs, reason = _segment_needs_permission(segment)
        if needs:
            requests.append((segment, reason))

    if not requests:
        return True, "OK"

    print("\n[PERMISSION] The AI wants to run:")
    for segment, reason in requests:
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

def request_file_permission(operation: str, path: str) -> Tuple[bool, str]:
    """Request permission for a direct file-system operation (read/write/find_replace).

    Used by ``tools.py`` for file operations that bypass the shell.  Returns
    ``(approved, reason)`` — when *approved* is ``True`` the caller may proceed.

    The api.keys secrets file is always gated regardless of autonomous mode.
    All other paths defer to the existing ``validate_command`` logic so that
    nothing is *automatically* rejected — the user is always asked first.
    """
    p = _expand_path(path)

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
        rel = os.path.relpath(p, AI_ROOT)
        return False, f"[PERMISSION DENIED] '{rel}': {reason}"
    return True, "OK"


def temp_path(name: str, session_id: str | None = None) -> str:
    sid = session_id or os.environ.get("TERMUX_AI_SESSION_ID") or "default"
    return os.path.join(TEMP_ROOT, sid, name)
