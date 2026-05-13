import os
import shlex

AI_ROOT = os.path.abspath(os.path.expanduser("~/ai_root"))
CORE_DIR = os.path.join(AI_ROOT, "core")

FORBIDDEN_KEYWORDS = [
    "sudo", "passwd", "ssh", "su",
    "nano", "vim", "vi", "less", "more", "read "
]


def is_inside_root(path: str) -> bool:
    try:
        abs_path = os.path.abspath(os.path.expanduser(path))
        return abs_path.startswith(AI_ROOT)
    except Exception:
        return False


def is_inside_core(path: str) -> bool:
    try:
        abs_path = os.path.abspath(os.path.expanduser(path))
        return abs_path.startswith(CORE_DIR)
    except Exception:
        return False


def extract_redirection_target(cmd: str):
    if ">" not in cmd:
        return None

    parts = cmd.rsplit(">", 1)
    if len(parts) != 2:
        return None

    target = parts[1].strip()
    if not target:
        return None

    try:
        return shlex.split(target)[0]
    except Exception:
        return None


def command_needs_permission(cmd: str) -> bool:
    lowered = cmd.lower()

    for word in FORBIDDEN_KEYWORDS:
        if word in lowered:
            return True

    target = extract_redirection_target(cmd)
    if target:
        if not is_inside_root(target):
            return True
        if is_inside_core(target):
            return True

    return False


def validate_command(cmd: str):
    if command_needs_permission(cmd):
        return False, "Command requires permission (unsafe or core-protected)"
    return True, "OK"
