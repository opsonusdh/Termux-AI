"""core/ui.py — Terminal chrome for the Termux-AI chat interface.

``core/renderer.py`` turns *content* into ANSI text: markdown, tables, code
blocks, inline emphasis.  That output is model-authored and has to stay
byte-faithful to what the model wrote.

This module turns *structure* into ANSI text: the banner, the status row,
borders around panels, coloured prompts, status notices.  That output is
app-authored and may be re-laid-out freely.

The split keeps the two concerns apart on purpose.  Nothing here parses
markdown, and nothing here renders model prose — the renderer is not
bypassed, and this module does not shadow it.

Every widget is terminal-width aware and degrades to a narrow, readable
layout on a phone-sized Termux window (often 40-60 columns).
"""
from __future__ import annotations

import os
import sys
import textwrap
from typing import Sequence

# Path bootstrap — same convention as every other cross-package module.
_CORE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_CORE)
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)
if _ROOT not in sys.path:
    sys.path.insert(1, _ROOT)

from core.renderer import (  # noqa: E402  (import after the path bootstrap)
    ANSI_ESCAPE,
    BOLD,
    CYAN,
    DIM,
    GRAY,
    GREEN,
    MAG,
    RED,
    RESET,
    YELLOW,
    display_width,
    pad_ansi_string,
    term_size,
    truncate_with_ellipsis,
)

# ── Semantic tones ─────────────────────────────────────────────────────────
# One place that decides what "green" means, so the interface reads as a
# single system instead of a dozen hand-picked escape sequences.

TONES: dict[str, str] = {
    "ok": GREEN,
    "warn": YELLOW,
    "error": RED,
    "info": CYAN,
    "accent": MAG,
    "muted": GRAY,
}

# Glyph + colour per notice kind.  Deliberately ASCII-ish/geometric: the
# Termux font on many devices has no emoji coverage, and a missing glyph
# renders as a tofu box.
_NOTICE_GLYPHS: dict[str, str] = {
    "ok": "✓",
    "warn": "!",
    "error": "✗",
    "info": "·",
    "muted": "·",
}

# Narrowest layout we will attempt to render before giving up on alignment.
_MIN_WIDTH = 24


# ── Width helpers ──────────────────────────────────────────────────────────

def term_width(default: int = 80) -> int:
    """Usable terminal width, never below ``_MIN_WIDTH``."""
    try:
        columns = term_size()[0]
    except Exception:
        columns = default
    if not columns or columns <= 0:
        columns = default
    return max(_MIN_WIDTH, columns)


def plain(text: str) -> str:
    """Strip ANSI escape sequences from *text*."""
    return ANSI_ESCAPE.sub("", text or "")


def wrap(text: str, width: int, indent: str = "") -> list[str]:
    """Word-wrap *text* to *width*, prefixing every line with *indent*."""
    width = max(8, width)
    if not text:
        return [indent.rstrip()] if indent else [""]
    out: list[str] = []
    for para in str(text).split("\n"):
        if not para.strip():
            out.append("")
            continue
        wrapped = textwrap.wrap(
            para,
            width=width,
            initial_indent=indent,
            subsequent_indent=indent,
            break_long_words=True,
            break_on_hyphens=False,
        )
        out.extend(wrapped or [indent.rstrip()])
    return out


def clip(text: str, width: int) -> str:
    """Truncate *text* to *width* display columns, adding an ellipsis."""
    return truncate_with_ellipsis(text, width)


# ── Text primitives ────────────────────────────────────────────────────────

def heading(text: str, tone: str = "accent") -> str:
    """A section heading: a coloured accent bar plus the label."""
    color = TONES.get(tone, TONES["accent"])
    return f"{color}▌{RESET} {BOLD}{text}{RESET}"


def notice(kind: str, message: str, indent: int = 2) -> str:
    """A one-line status message with a coloured glyph.

    Long messages wrap under the text column instead of running off the
    right edge of a phone-sized terminal.
    """
    glyph = _NOTICE_GLYPHS.get(kind, _NOTICE_GLYPHS["info"])
    color = TONES.get(kind, TONES["info"])
    pad = " " * indent
    width = max(8, term_width() - indent - 2)
    lines = wrap(message, width)
    if not lines:
        return ""
    out = [f"{pad}{color}{glyph}{RESET} {lines[0]}"]
    for extra in lines[1:]:
        out.append(f"{pad}  {extra}")
    return "\n".join(out)


def status_pill(label: str, value: str, tone: str = "muted") -> str:
    """A single inline status badge, e.g. ``mode AUTONOMOUS``."""
    color = TONES.get(tone, TONES["muted"])
    return f"{GRAY}{label}{RESET} {color}{BOLD}{value}{RESET}"


def status_row(pills: Sequence[tuple[str, str, str]], indent: int = 2) -> str:
    """A row of inline status badges.

    Each pill is ``(label, value, tone)``.  The row wraps between pills when
    the terminal is too narrow to hold them all on one line.
    """
    if not pills:
        return ""
    pad = " " * indent
    sep = f"   {GRAY}·{RESET} "
    rendered = [status_pill(label, value, tone) for label, value, tone in pills]
    line = pad + sep.join(rendered)
    if display_width(line) <= term_width():
        return line
    # Too narrow for one line: break between pills rather than overflowing.
    out: list[str] = []
    current = pad
    for pill in rendered:
        candidate = current if current == pad else current + sep
        candidate += pill
        if display_width(candidate) > term_width() and current != pad:
            out.append(current)
            current = pad + pill
        else:
            current = candidate
    if current.strip():
        out.append(current)
    return "\n".join(out)


def prompt(label: str, tone: str = "info") -> str:
    """A coloured input prompt, e.g. ``YOU >``."""
    color = TONES.get(tone, TONES["info"])
    return f"{color}{BOLD}{label}{RESET} {GRAY}›{RESET} "


# ── Panels ─────────────────────────────────────────────────────────────────

def panel(
    title: str,
    rows: Sequence[str],
    *,
    indent: int = 2,
    tone: str = "accent",
    min_width: int = 0,
) -> str:
    """A bordered panel sized to its content and capped at the terminal width.

    *rows* may contain ANSI styling — widths are measured on the visible
    text, so coloured cells still line up.
    """
    if not rows:
        rows = []
    pad = " " * indent
    color = TONES.get(tone, TONES["accent"])

    content_width = max((display_width(r) for r in rows), default=0)
    if title:
        # "─ Title " is the part of the top border the title occupies.
        content_width = max(content_width, display_width(title) + 2)
    content_width = max(content_width, min_width)

    # Border + one space of padding on each side.
    inner_width = content_width + 2
    total_width = inner_width + 2
    available = term_width() - indent
    if total_width > available:
        inner_width = max(1, available - 2)
        content_width = max(1, inner_width - 2)

    if title:
        head_fill = max(0, inner_width - display_width(title) - 2)
        top = f"{color}┌─{RESET} {BOLD}{title}{RESET} {color}{'─' * head_fill}┐{RESET}"
    else:
        top = f"{color}┌{'─' * inner_width}┐{RESET}"

    body: list[str] = []
    for row in rows:
        visible = display_width(row)
        if visible > content_width:
            row = clip(row, content_width)
            visible = display_width(row)
        padded = pad_ansi_string(row, content_width)
        body.append(f"{color}│{RESET} {padded} {color}│{RESET}")

    bottom = f"{color}└{'─' * inner_width}┘{RESET}"
    return "\n".join([pad + top, *body, pad + bottom])


def key_value_rows(
    pairs: Sequence[tuple[str, str]],
    *,
    key_width: int | None = None,
    indent: int = 2,
    tone: str = "info",
) -> list[str]:
    """Aligned ``key  value`` rows for a help listing or a status table."""
    if not pairs:
        return []
    if key_width is None:
        key_width = max(display_width(k) for k, _ in pairs)
    color = TONES.get(tone, TONES["info"])
    pad = " " * indent
    gap = 2
    rows: list[str] = []
    for key, value in pairs:
        visible_key = display_width(key)
        if visible_key > key_width:
            key = clip(key, key_width)
        # Pad the *visible* key, not the escape sequence.
        key_cell = pad_ansi_string(f"{color}{key}{RESET}", key_width + display_width(key) - visible_key)
        rows.append(f"{pad}{key_cell}{' ' * gap}{value}")
    return rows


def banner(name: str, subtitle: str = "", tagline: str = "",
           version: str = "") -> str:
    """The application header shown once at startup.

    ``version`` is printed on its own line between the name and the
    subtitle, so the header reads:

        ▌ Termux-AI
          v3.4.0
          Terminal AI ready.
    """
    lines = [f"{CYAN}{BOLD}▌{RESET} {BOLD}{name}{RESET}"]
    if version:
        text = version if version.startswith("v") else f"v{version}"
        lines.append(f"  {DIM}{text}{RESET}")
    if subtitle:
        lines.append(f"  {GRAY}{subtitle}{RESET}")
    if tagline:
        lines.append("")
        lines.extend(wrap(tagline, term_width() - 2, "  "))
    return "\n".join(lines)


def rule(char: str = "─", tone: str = "muted", indent: int = 0) -> str:
    """A horizontal rule spanning the terminal width."""
    color = TONES.get(tone, TONES["muted"])
    return (" " * indent) + color + char * max(1, term_width() - indent) + RESET


def hint(text: str, indent: int = 2) -> str:
    """A dimmed hint line, e.g. the keyboard shortcuts."""
    pad = " " * indent
    return "\n".join(f"{pad}{GRAY}{line}{RESET}" for line in wrap(text, term_width() - indent))
