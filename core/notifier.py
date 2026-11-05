"""core/notifier.py — push notifications for the prompts that block the user.

Three moments make the runtime stop and wait for a human:

* a **permission** request (the shell/file gate asks "allow all listed
  actions? [y/n]"),
* a **confirmation** (``confirm``),
* a **question** (``ask_user``).

When the terminal is in the background — which is the normal state on a
phone — those prompts are invisible: the runtime sits on ``input()`` and
nothing on screen says so.  With ``notify`` enabled in ``config.json`` each
of those moments pushes a system notification, so the user learns that the
AI is waiting instead of finding out minutes later.

Each kind of prompt gets its own title, so the notification shade says *what*
is being asked rather than a generic "Termux-AI".

The module depends on nothing but the standard library plus ``core.app_config``
and ``paths``, so it can be imported from anywhere without creating a cycle.
It never writes to stdout: the notification is fired while the prompt is on
screen, and a stray print there would land in the middle of the user's line.
"""
from __future__ import annotations

import subprocess
import sys

# Path bootstrap — same convention as every other cross-package module.
import os

_CORE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_CORE)
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)
if _ROOT not in sys.path:
    sys.path.insert(1, _ROOT)

from core import app_config  # noqa: E402


# ── Titles ───────────────────────────────────────────────────────────────────
# One per kind of prompt, so the shade entry says what is being asked.
# Plain ASCII on purpose: the Termux notification font is not guaranteed to
# carry every glyph, and a missing one renders as a tofu box.
TITLE_PERMISSION = "Termux-AI: Permission needed"
TITLE_CONFIRMATION = "Termux-AI: Confirmation needed"
TITLE_QUESTION = "Termux-AI: Question"

#: How long the notification command may take.  The runtime is about to block
#: on ``input()``, so a wedged notification must never delay the prompt.
_TIMEOUT = 5

#: Longest notification body.  Android truncates long notifications anyway,
#: and a wall of text in the shade is unreadable.
_MAX_CONTENT = 500


def enabled() -> bool:
    """True when the ``notify`` flag is on in ``config.json``.

    Read from disk every time: the user can switch it off mid-session with
    ``/notify false``, and the next prompt has to honour that.
    """
    try:
        return bool(app_config.load_config().get("notify", True))
    except Exception:
        return False


def push(title: str, content: str) -> bool:
    """Send one notification.  Returns True when it was actually sent.

    Never raises and never prints: a missing Termux:API package or a broken
    notification service must not stop the runtime from asking its question.
    """
    if not enabled():
        return False
    body = (content or "").strip()
    if not body:
        return False
    if len(body) > _MAX_CONTENT:
        body = body[:_MAX_CONTENT - 3].rstrip() + "..."
    try:
        subprocess.run(
            ["termux-notification", "--title", str(title), "--content", body, "--priority", "high"],
            capture_output=True,
            timeout=_TIMEOUT,
        )
        return True
    except Exception:
        return False


# ── The three kinds of prompt ────────────────────────────────────────────────

def permission(requests) -> bool:
    """Notify that the permission gate is waiting for an answer.

    *requests* is the list of ``(command, reason)`` pairs the gate collected.
    """
    items = list(requests or [])
    if not items:
        return False
    lines = []
    for segment, reason in items:
        lines.append(f"• {segment}")
        if reason:
            lines.append(f"  {reason}")
    body = (
        "The AI wants to run:\n" + "\n".join(lines)
        + "\n\nOpen Termux-AI and answer y/n."
    )
    return push(TITLE_PERMISSION, body)


def confirmation(question: str, default: bool = True) -> bool:
    """Notify that a yes/no confirmation is waiting."""
    hint = "press Enter for Yes" if default else "press Enter for No"
    body = f"{question}\n\n({hint}) — open Termux-AI to answer."
    return push(TITLE_CONFIRMATION, body)


def question(text: str, options=None) -> bool:
    """Notify that a question is waiting for an answer."""
    body = text or ""
    if options:
        listed = "\n".join(
            f"  {i}. {opt}" for i, opt in enumerate(options, 1)
        )
        body += f"\n\nOptions:\n{listed}"
    body += "\n\nOpen Termux-AI to answer."
    return push(TITLE_QUESTION, body)
