"""
permissions.py – Autonomy-first permission gate for ~/ai_root AI agent.

Philosophy
----------
The AI operates freely inside ~/ai_root.  Permission is requested only when
an action would:

  1. Run a forbidden system command (sudo, reboot, …)
  2. Write into core/         – the AI's own source code
  3. Write into Termux-STT/   – the speech-to-text module
  4. Write/execute outside ~/ai_root entirely

Everything else – workspace edits, package installs, reading files, running
scripts, piping data, even installing packages – proceeds without interruption.
"""

import os
import re
import sys
import json
import shlex
import subprocess
from typing import List, Tuple

#  Directory roots 
AI_ROOT   = os.path.abspath(os.path.expanduser("~/ai_root"))
CORE_DIR  = os.path.join(AI_ROOT, "core")
STT_DIR   = os.path.join(AI_ROOT, "Termux-STT")
WORKSPACE = os.path.join(AI_ROOT, "workspace")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DEFAULT_CONFIG = {
    "stt_path": os.path.join(BASE_DIR, "Termux-STT"),
    "tts_enabled": False,
}

if not os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=4)

def is_voice_available():
    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
    except Exception:
        config = DEFAULT_CONFIG

    if not config.get("tts_enabled", False):
        return False

    STT_PATH = os.path.expanduser(config["stt_path"])
    if STT_PATH not in sys.path:
        sys.path.append(STT_PATH)

    try:
        from main import listen

        if subprocess.run(
            "which edge-tts",
            shell=True,
            capture_output=True
        ).returncode != 0:
            return False
        if subprocess.run(
            "which mpv",
            shell=True,
            capture_output=True
        ).returncode != 0:
            return False

        return True

    except Exception:
        return False

# Directories the AI must never silently modify
PROTECTED_DIRS: List[str] = [CORE_DIR, STT_DIR]

#  Command categories 

# Unconditionally blocked – these touch system state no AI should touch.
FORBIDDEN_COMMANDS = {
    "sudo", "su", "pkexec", "passwd",
    "shutdown", "reboot", "poweroff", "halt",
    "mount", "umount",
}

# Pure read-only commands – they never create or change files on their own.
# NOTE: we still check their output redirections below (echo "x" > core/f).
READ_ONLY_COMMANDS = {
    "cat", "ls", "pwd", "whoami", "id", "echo", "printf",
    "head", "tail", "grep", "find", "wc", "sort", "uniq",
    "awk", "sed", "tree", "which", "command", "stat", "du",
    "file", "basename", "dirname", "realpath", "date", "env",
    "printenv", "diff", "less", "more", "type", "ldd", "strings",
}

# Commands that modify the filesystem – checked against target paths.
MUTATING_COMMANDS = {
    "rm", "rmdir", "mv", "cp", "mkdir", "touch",
    "truncate", "tee", "ln", "chmod", "chown",
}

# Script/code interpreters – checked against what they are given to run.
INTERPRETER_COMMANDS = {
    "python", "python3", "bash", "sh", "zsh",
    "node", "nodejs", "perl", "ruby",
}

# Package managers – installing software is a normal, routine AI task.
# They don't touch ai_root source directly, so they run freely.
PACKAGE_MANAGERS = {
    "pip", "pip3", "npm", "pkg", "apt", "apt-get",
    "yarn", "npx", "gem", "cargo", "pipx",
}

# Flags that accept a file output argument (wget -O, curl -o, etc.)
_OUTPUT_FLAGS = {"-o", "-O", "--output"}

# Regex patterns that flag dangerous inline (-c) code
_DANGEROUS_INLINE_PATTERNS = [
    re.compile(r'\brm\b[^;|&\n]*-[a-zA-Z]*r'),  # recursive rm
    re.compile(r':\s*\(\s*\)\s*\{'),              # fork bomb  :(){…}
    re.compile(r'\bdd\b.*\bof=/'),                # raw device overwrite
]


#  Path utilities 

def _commonpath_is_inside(base: str, path: str) -> bool:
    try:
        base = os.path.abspath(os.path.expanduser(base))
        path = os.path.abspath(os.path.expanduser(path))
        return os.path.commonpath([base, path]) == base
    except Exception:
        return False

def _expand_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))

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
    """True when path escapes ~/ai_root entirely."""
    return not _commonpath_is_inside(AI_ROOT, _expand_path(path))

def _is_pathish(token: str) -> bool:
    """Heuristic: does this token look like a file-system path?"""
    if not token or token.startswith("-"):
        return False
    return "/" in token or token.startswith("~") or token.startswith(".")

def _path_verdict(path: str) -> Tuple[bool, str]:
    """
    Core sensitivity judgement for a single path.
    Returns (needs_permission, human-readable reason).
    """
    if _is_protected(path):
        rel = os.path.relpath(_expand_path(path), AI_ROOT)
        protected_name = next(
            os.path.basename(d) for d in PROTECTED_DIRS
            if _commonpath_is_inside(d, _expand_path(path))
        )
        return True, f"path '{rel}' is inside protected directory '{protected_name}/'"
    if _is_outside_root(path):
        return True, f"path '{path}' is outside ~/ai_root"
    return False, "OK"


#  Shell parsing helpers 

def _split_shell_chain(cmd: str) -> List[str]:
    """Split a shell command string on ;  &&  ||  and newlines."""
    parts: List[str] = []
    buf:   List[str] = []
    quote = None
    escape = False
    i = 0

    while i < len(cmd):
        ch = cmd[i]
        if escape:
            buf.append(ch); escape = False; i += 1; continue
        if ch == "\\":
            buf.append(ch); escape = True; i += 1; continue
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1; continue
        if ch in ("'", '"'):
            buf.append(ch); quote = ch; i += 1; continue
        if ch in ("\n", ";"):
            seg = "".join(buf).strip()
            if seg: parts.append(seg)
            buf = []; i += 1; continue
        if ch == "&" and i + 1 < len(cmd) and cmd[i + 1] == "&":
            seg = "".join(buf).strip()
            if seg: parts.append(seg)
            buf = []; i += 2; continue
        if ch == "|" and i + 1 < len(cmd) and cmd[i + 1] == "|":
            seg = "".join(buf).strip()
            if seg: parts.append(seg)
            buf = []; i += 2; continue
        buf.append(ch); i += 1

    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts

def _safe_split(cmd: str) -> List[str]:
    try:
        return shlex.split(cmd, posix=True)
    except Exception:
        return []

def _cmd_name(segment: str) -> str:
    tokens = _safe_split(segment)
    return os.path.basename(tokens[0]).lower() if tokens else ""

def _extract_write_targets(tokens: List[str]) -> List[str]:
    """
    Return paths that shell *output* redirections write to.
    Deliberately excludes < and << (input redirections) so reading
    from a protected file is still allowed.
    """
    WRITE_OPS = {">", ">>", "1>", "1>>", "2>", "2>>", "&>", "&>>"}
    targets: List[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in WRITE_OPS:
            if i + 1 < len(tokens):
                targets.append(tokens[i + 1]); i += 2; continue
        m = re.match(r"^(?:\d*>>?|&>>?)(.+)$", tok)
        if m:
            tgt = m.group(1).strip()
            if tgt: targets.append(tgt)
        i += 1
    return targets

def _extract_output_flag_targets(tokens: List[str]) -> List[str]:
    """Extract file paths from -o / -O / --output flags (curl, wget, …)."""
    targets: List[str] = []
    i = 1
    while i < len(tokens):
        if tokens[i] in _OUTPUT_FLAGS and i + 1 < len(tokens):
            targets.append(tokens[i + 1]); i += 2
        else:
            i += 1
    return targets


#  Per-category permission checks 

def _check_write_targets(cmd: str, targets: List[str]) -> Tuple[bool, str]:
    """Shared helper: veto if any target in the list is sensitive."""
    for t in targets:
        needs, reason = _path_verdict(t)
        if needs:
            return True, f"'{cmd}': {reason}"
    return False, "OK"

def _check_mutating(cmd: str, tokens: List[str]) -> Tuple[bool, str]:
    """
    File-mutating commands need permission only when their target paths
    are protected or outside ~/ai_root.  Writing freely to workspace/
    or any other ai_root subdirectory is fine.
    """
    targets: List[str] = []

    if cmd == "tee":
        # tee writes to its positional (non-flag) args
        targets = [t for t in tokens[1:] if not t.startswith("-")]

    elif cmd in {"rm", "rmdir", "mkdir", "touch", "truncate", "ln",
                 "chmod", "chown"}:
        targets = [t for t in tokens[1:] if _is_pathish(t)]

    elif cmd in {"mv", "cp"}:
        # destination is the last path-like arg
        path_args = [t for t in tokens[1:] if _is_pathish(t)]
        targets = path_args  # check all; destination matters most

    # Shell redirections can also be the write target
    targets += _extract_write_targets(tokens)

    # rm/rmdir with no recoverable path → be cautious
    if cmd in {"rm", "rmdir"} and not targets:
        return True, f"'{cmd}' with no verifiable target path"

    return _check_write_targets(cmd, targets)

def _check_interpreter(cmd: str, tokens: List[str]) -> Tuple[bool, str]:
    """
    Interpreters are checked against *what* they run, not blanket-blocked.

    • python -m …           → module runner (fine, covers pip via -m pip)
    • python/bash -c "…"    → inline code scanned for protected refs & dangers
    • python script.py      → script location checked
    • bare python/bash      → interactive shell, fine
    """
    i = 1
    while i < len(tokens):
        tok = tokens[i]

        # -m flag: treat as a module/package-manager invocation – always fine
        if tok == "-m":
            return False, "OK"

        # -c flag: analyse the inline code string
        if tok == "-c" and i + 1 < len(tokens):
            inline = tokens[i + 1]

            # Protected directory name/path mentioned in the code
            for pdir in PROTECTED_DIRS:
                name = os.path.basename(pdir)
                if pdir in inline or name in inline:
                    return True, (
                        f"inline code references protected directory '{name}/'"
                    )

            # Dangerous patterns (recursive rm, fork bomb, …)
            for pat in _DANGEROUS_INLINE_PATTERNS:
                if pat.search(inline):
                    return True, "inline code contains a potentially destructive pattern"

            return False, "OK"

        # Skip other flags
        if tok.startswith("-"):
            i += 1; continue

        # First non-flag argument is the script file
        needs, reason = _path_verdict(tok)
        if needs:
            return True, f"'{cmd}': {reason}"
        return False, "OK"

        i += 1  # unreachable; keeps linter happy

    # Bare interpreter with no arguments (interactive) – fine
    return False, "OK"

def _check_generic(cmd: str, tokens: List[str]) -> Tuple[bool, str]:
    """
    Catch-all for commands not in any other category.
    Only blocks when output (via shell redirection or -o/-O flags)
    lands in a protected directory or outside ~/ai_root.
    """
    targets = _extract_write_targets(tokens) + _extract_output_flag_targets(tokens)
    return _check_write_targets(cmd, targets)


#  Public API 

def _segment_needs_permission(segment: str) -> Tuple[bool, str]:
    tokens = _safe_split(segment)
    if not tokens:
        return False, "OK"

    cmd = _cmd_name(segment)

    #  1. Hard stop 
    if cmd in FORBIDDEN_COMMANDS:
        return True, f"'{cmd}' is a forbidden system command"

    #  2. Pure read-only 
    # Still guard write redirections: `echo x > core/file` must be caught.
    if cmd in READ_ONLY_COMMANDS:
        write_targets = _extract_write_targets(tokens)
        if write_targets:
            return _check_write_targets(cmd, write_targets)
        return False, "OK"

    #  3. Package managers 
    # Installing/removing packages is a routine, low-risk AI task.
    if cmd in PACKAGE_MANAGERS:
        return False, "OK"

    #  4. File-mutating commands 
    if cmd in MUTATING_COMMANDS:
        return _check_mutating(cmd, tokens)

    #  5. Interpreters 
    if cmd in INTERPRETER_COMMANDS:
        return _check_interpreter(cmd, tokens)

    #  6. Everything else 
    # Default is ALLOW.  Only veto if output clearly writes to a sensitive path.
    return _check_generic(cmd, tokens)


def command_needs_permission(cmd: str) -> bool:
    """Quick boolean check – does this command chain need user confirmation?"""
    return any(
        _segment_needs_permission(seg)[0]
        for seg in _split_shell_chain(cmd)
    )


def validate_command(cmd: str) -> Tuple[bool, str]:
    """
    Gate a full command string.

    Returns (allowed: bool, reason: str).
    Prompts the user only for genuinely sensitive operations; everything else
    passes through silently.
    """
    for segment in _split_shell_chain(cmd):
        needs, reason = _segment_needs_permission(segment)

        if needs:
            print(f"\n[PERMISSION] The AI wants to run:\n  {segment}")
            print(f"  Reason: {reason}")

            # Voice notification
            try:
                if is_voice_available():
                    subprocess.Popen(
                        (
                            'edge-tts '
                            '--voice "en-US-AndrewNeural" '
                            f'--text "Permission required. Reason: {reason}" '
                            '--write-media - | mpv -'
                        ),
                        shell=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
            except Exception:
                pass

            inp = input("  Allow? [y/n] ").strip().lower()

            if inp in {"y", "yes"}:
                return True, "OK"

            return False, f"Denied: {reason}"

    return True, "OK"