import os
import re
import shlex
from typing import List, Tuple

AI_ROOT = os.path.abspath(os.path.expanduser("~/ai_root"))
CORE_DIR = os.path.join(AI_ROOT, "core")
WORKSPACE_DIR = os.path.join(AI_ROOT, "workspace")

FORBIDDEN_COMMANDS = {
    "sudo", "su", "pkexec", "passwd", "shutdown", "reboot",
    "poweroff", "halt", "mount", "umount",
}

ALWAYS_CONFIRM_COMMANDS = {
    "python", "python3", "pip", "pip3",
    "node", "npm", "bash", "sh", "zsh",
    "pkg", "apt", "apt-get",
}

READ_ONLY_COMMANDS = {
    "cat", "ls", "pwd", "whoami", "id", "echo", "printf",
    "head", "tail", "grep", "find", "wc", "sort", "uniq",
    "awk", "sed", "tree", "which", "command", "stat", "du",
    "file", "basename", "dirname", "realpath", "date", "env",
    "printenv",
}

def _commonpath_is_inside(base: str, path: str) -> bool:
    try:
        base = os.path.abspath(os.path.expanduser(base))
        path = os.path.abspath(os.path.expanduser(path))
        return os.path.commonpath([base, path]) == base
    except Exception:
        return False

def is_inside_root(path: str) -> bool:
    return _commonpath_is_inside(AI_ROOT, path)

def is_inside_core(path: str) -> bool:
    return _commonpath_is_inside(CORE_DIR, path)

def is_inside_workspace(path: str) -> bool:
    return _commonpath_is_inside(WORKSPACE_DIR, path)

def _split_shell_chain(cmd: str) -> List[str]:
    parts = []
    buf = []
    quote = None
    escape = False
    i = 0

    while i < len(cmd):
        ch = cmd[i]

        if escape:
            buf.append(ch)
            escape = False
            i += 1
            continue

        if ch == "\\":
            buf.append(ch)
            escape = True
            i += 1
            continue

        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue

        if ch in ("'", '"'):
            buf.append(ch)
            quote = ch
            i += 1
            continue

        if ch == "\n" or ch == ";":
            segment = "".join(buf).strip()
            if segment:
                parts.append(segment)
            buf = []
            i += 1
            continue

        if ch == "&" and i + 1 < len(cmd) and cmd[i + 1] == "&":
            segment = "".join(buf).strip()
            if segment:
                parts.append(segment)
            buf = []
            i += 2
            continue

        if ch == "|" and i + 1 < len(cmd) and cmd[i + 1] == "|":
            segment = "".join(buf).strip()
            if segment:
                parts.append(segment)
            buf = []
            i += 2
            continue

        buf.append(ch)
        i += 1

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
    if not tokens:
        return ""
    return os.path.basename(tokens[0]).lower()

def _expand_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))

def _is_pathish(token: str) -> bool:
    if not token or token.startswith("-"):
        return False
    return (
        "/" in token
        or token.startswith("~")
        or token.startswith(".")
    )

def _extract_redirection_targets(tokens: List[str]) -> List[str]:
    targets = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok in {">", ">>", "1>", "1>>", "2>", "2>>", "&>", "&>>", "<", "<<"}:
            if i + 1 < len(tokens):
                targets.append(tokens[i + 1])
                i += 2
                continue

        m = re.match(r"^(?:\d*>>?|\&>>?|\<<?)(.+)$", tok)
        if m:
            tgt = m.group(1).strip()
            if tgt:
                targets.append(tgt)

        i += 1

    return targets

def _all_targets_allowed(targets: List[str]) -> bool:
    """
    Allowed means:
    - inside ~/ai_root/workspace
    - not inside ~/ai_root/core
    """
    if not targets:
        return False

    for t in targets:
        path = _expand_path(t)
        if not is_inside_workspace(path):
            return False
        if is_inside_core(path):
            return False

    return True

def _segment_needs_permission(segment: str) -> Tuple[bool, str]:
    tokens = _safe_split(segment)
    if not tokens:
        return False, "OK"

    cmd = _cmd_name(segment)

    if cmd in FORBIDDEN_COMMANDS:
        return True, f"Command '{cmd}' is forbidden"

    if cmd in READ_ONLY_COMMANDS:
        return False, "OK"

    if cmd in ALWAYS_CONFIRM_COMMANDS:
        return True, f"Command '{cmd}' needs confirmation"

    # Generic workspace-based allow rule.
    # If the command writes/deletes only inside workspace, allow it.
    mutating_prefixes = {
        "rm", "rmdir", "mv", "cp", "mkdir", "touch",
        "truncate", "tee", "ln", "chmod", "chown"
    }

    if cmd in mutating_prefixes:
        # Special handling for commands that often write via redirection or output flags
        targets = []

        # file path args after typical mutating commands
        if cmd in {"rm", "rmdir", "mkdir", "touch", "truncate", "ln"}:
            targets.extend([t for t in tokens[1:] if _is_pathish(t)])

        elif cmd in {"mv", "cp"}:
            targets.extend([t for t in tokens[1:] if _is_pathish(t)])

        elif cmd == "tee":
            targets.extend(_extract_redirection_targets(tokens))
            
        else:
            targets.extend(_extract_redirection_targets(tokens))

        if targets and _all_targets_allowed(targets):
            return False, "OK"

        # If we cannot verify a safe workspace target, ask.
        return True, f"'{cmd}' may write outside workspace or target could not be verified"

    # Generic path-based redirection check for other commands
    redir_targets = _extract_redirection_targets(tokens)
    if redir_targets:
        if _all_targets_allowed(redir_targets):
            return False, "OK"
        return True, "Redirection target is outside workspace or inside core"

    # Default: allow harmless commands, ask for everything else
    if cmd in {"python", "python3", "bash", "sh"}:
        return True, f"Command '{cmd}' is too general to trust without confirmation"

    return False, "OK"

def command_needs_permission(cmd: str) -> bool:
    for segment in _split_shell_chain(cmd):
        needs, _reason = _segment_needs_permission(segment)
        if needs:
            return True
    return False

def validate_command(cmd: str):
    """
    Returns (allowed, reason).
    Commands that are safe or constrained to workspace are allowed.
    Risky commands ask the user.
    """
    for segment in _split_shell_chain(cmd):
        needs, reason = _segment_needs_permission(segment)
        if needs:
            inp = input(
                f"[PERMISSION] AI is trying to run:\n{segment}\n"
                f"Reason: {reason}\nAllow? [y/n] "
            ).strip().lower()
            if inp in {"y", "yes"}:
                return True, "OK"
            return False, f"Command requires permission: {reason}"

    return True, "OK"