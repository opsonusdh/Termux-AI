import re

#  ANSI colour codes

RESET  = "\033[0m"

BOLD   = "\033[1m"
ITALIC = "\033[3m"
DIM    = "\033[2m"

RED    = "\033[31m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
BLUE   = "\033[34m"
MAG    = "\033[35m"
CYAN   = "\033[36m"
GRAY   = "\033[90m"

DIVIDER = "─" * 38


# Inline markdown regex (shared by both renderers)

BOLD_RE        = re.compile(r"\*\*(.+?)\*\*")
ITALIC_RE      = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
INLINE_CODE_RE = re.compile(r"`([^`]+)`")


#  TERMINAL RENDERER

def render_inline(text: str) -> str:
    """Apply inline markdown formatting (colour + style) for terminal output."""

    # inline code first (avoids collisions with bold/italic)
    text = INLINE_CODE_RE.sub(
        lambda m: f"{YELLOW}`{m.group(1)}`{RESET}",
        text,
    )
    text = BOLD_RE.sub(lambda m: f"{BOLD}{m.group(1)}{RESET}", text)
    text = ITALIC_RE.sub(lambda m: f"{ITALIC}{m.group(1)}{RESET}", text)
    return text


def render_markdown_terminal(text: str) -> str:
    """Transform markdown into ANSI-coloured terminal output."""

    lines    = text.splitlines()
    rendered = []
    in_code  = False
    code_lines = []

    for line in lines:
        stripped = line.strip()

        # Code block open / close
        if stripped.startswith("```"):
            if in_code:
                width  = max((len(x) for x in code_lines), default=20)
                border = "─" * min(max(width, 20), 80)
                rendered.append(f"{GRAY}┌─ code {border}{RESET}")
                for code_line in code_lines:
                    rendered.append(f"{GRAY}{code_line}{RESET}")
                rendered.append(f"{GRAY}└{'─' * (len(border) + 8)}{RESET}")
                code_lines = []
                in_code    = False
            else:
                in_code = True
            continue

        # Inside code block
        if in_code:
            code_lines.append(line.rstrip())
            continue

        # Divider
        if stripped == "---":
            rendered.append(f"{GRAY}{DIVIDER}{RESET}")
            continue

        # Headings
        if stripped.startswith("# "):
            rendered.append(f"{BOLD}{BLUE}{render_inline(stripped[2:])}{RESET}")
            continue
        if stripped.startswith("## "):
            rendered.append(f"{BOLD}{CYAN}{render_inline(stripped[3:])}{RESET}")
            continue
        if stripped.startswith("### "):
            rendered.append(f"{BOLD}{MAG}{render_inline(stripped[4:])}{RESET}")
            continue

        # Bullet lists
        if re.match(r"^\s*[-*•]\s+", line):
            bullet_line = re.sub(r"^(\s*)[-*•]\s+", rf"\1{GREEN}• {RESET}", line)
            rendered.append(render_inline(bullet_line))
            continue

        # Numbered lists
        if re.match(r"^\s*\d+\.\s+", line):
            numbered = re.sub(r"^(\s*)(\d+\.)\s+", rf"\1{CYAN}\2{RESET} ", line)
            rendered.append(render_inline(numbered))
            continue

        # Empty line
        if stripped == "":
            rendered.append("")
            continue

        # Normal text
        rendered.append(render_inline(line))

    # Unclosed code block (safety net)
    if in_code and code_lines:
        width  = max((len(x) for x in code_lines), default=20)
        border = "─" * min(max(width, 20), 80)
        rendered.append(f"{GRAY}┌─ code {border}{RESET}")
        for code_line in code_lines:
            rendered.append(f"{GRAY}{code_line}{RESET}")
        rendered.append(f"{GRAY}└{'─' * (len(border) + 8)}{RESET}")

    return "\n".join(rendered)


#  VOICE / TTS SANITISER


# Voice config

_VOICE_SHORT_LINES = 3    # text-fence content at or below this → read aloud
_VOICE_SHORT_CHARS = 200  # text-fence content at or below this → read aloud

_CODE_PLACEHOLDER = "You can see the code in our conversation history."
_TEXT_PLACEHOLDER = "You can see the text in our conversation history."
_TABLE_PLACEHOLDER = "See the table in our conversation history."

# Fence labels treated as plain text (length-checked before speaking)
_TEXT_FENCE_LABELS = {"text", "txt", ""}

# Additional regex for voice

_STRIKE_RE    = re.compile(r"~~(.+?)~~")
_LINK_RE      = re.compile(r"\[([^\]]+)\]\([^\)]*\)")
_BARE_URL_RE  = re.compile(r"https?://\S+")
_DIVIDER_RE   = re.compile(r"^[-*_]{3,}$")
_TABLE_ROW_RE = re.compile(r"^\|.+\|[ \t]*$")


# Helpers

def _is_short_content(lines: list) -> bool:
    """True when block content is compact enough to be read aloud."""
    joined = " ".join(lines).strip()
    return len(lines) <= _VOICE_SHORT_LINES and len(joined) <= _VOICE_SHORT_CHARS


def _shorten_url(url: str) -> str:
    """Reduce a bare URL to its hostname for clean TTS output."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc
        return host if host else url
    except Exception:
        return url


def _strip_inline(text: str) -> str:
    """
    Strip all inline markdown markers and return plain, speakable text.

    Processes in this order to avoid partial matches:
        links → URLs → strikethrough → inline code → bold → italic
    """
    text = _LINK_RE.sub(r"\1", text)                                  # [label](url) → label
    text = _BARE_URL_RE.sub(lambda m: _shorten_url(m.group(0)), text) # https://… → domain
    text = _STRIKE_RE.sub(r"\1", text)                                 # ~~text~~ → text
    text = INLINE_CODE_RE.sub(r"\1", text)                             # `code` → code
    text = BOLD_RE.sub(r"\1", text)                                    # **bold** → bold
    text = ITALIC_RE.sub(r"\1", text)                                  # *italic* → italic
    return text


def _preprocess_tables(text: str) -> str:
    """
    Replace markdown tables with a short spoken placeholder before
    line-by-line processing begins.

    A table is two or more consecutive lines whose first and last
    non-whitespace character is '|'.
    """
    lines  = text.splitlines(keepends=True)
    result = []
    buf    = []

    def flush():
        if len(buf) >= 2:
            result.append(_TABLE_PLACEHOLDER + "\n")
        else:
            result.extend(buf)
        buf.clear()

    for line in lines:
        if _TABLE_ROW_RE.match(line.rstrip()):
            buf.append(line)
        else:
            if buf:
                flush()
            result.append(line)

    if buf:
        flush()

    return "".join(result)


# Main voice renderer

def render_for_voice(text: str) -> str:
    """
    Sanitise markdown for voice / TTS output.

    Returns plain text suitable for passing directly to a speech engine
    (e.g. pyttsx3, espeak, Whisper TTS, gTTS).

    See module docstring for the full transformation table.
    """
    # Tables span multiple lines – easiest handled before the main loop.
    text = _preprocess_tables(text)

    lines      = text.splitlines()
    output     = []
    in_code    = False
    fence_lang = ""
    code_lines = []

    for line in lines:
        stripped = line.strip()

        # Fenced block open / close
        if stripped.startswith("```"):
            if in_code:
                # Closing fence ─ decide what to emit
                if fence_lang in _TEXT_FENCE_LABELS:
                    clean = [l.strip() for l in code_lines if l.strip()]
                    if _is_short_content(clean):
                        content = " ".join(clean)
                        if content:
                            output.append(content)
                    else:
                        output.append(_TEXT_PLACEHOLDER)
                else:
                    # Any programming / data language → always replace
                    output.append(_CODE_PLACEHOLDER)

                code_lines = []
                fence_lang = ""
                in_code    = False
            else:
                # Opening fence ─ capture language tag
                fence_lang = stripped[3:].strip().lower()
                in_code    = True
            continue

        # Inside fenced block
        if in_code:
            code_lines.append(line.rstrip())
            continue

        # Dividers (---, ***, ___)
        if _DIVIDER_RE.match(stripped):
            continue

        # Empty lines (collapse consecutive blanks)
        if stripped == "":
            if output and output[-1] != "":
                output.append("")
            continue

        # Headings
        if stripped.startswith("### "):
            output.append(_strip_inline(stripped[4:]))
            continue
        if stripped.startswith("## "):
            output.append(_strip_inline(stripped[3:]))
            continue
        if stripped.startswith("# "):
            # H1 is uppercased so TTS engines naturally stress it more
            output.append(_strip_inline(stripped[2:]).upper())
            continue

        # Bullet lists
        m = re.match(r"^(\s*)[-*•]\s+(.+)$", line)
        if m:
            output.append(_strip_inline(m.group(2)))
            continue

        # Numbered lists
        m = re.match(r"^\s*(\d+)\.\s+(.+)$", line)
        if m:
            # "1. item" reads naturally: "one. item"
            output.append(f"{m.group(1)}. {_strip_inline(m.group(2))}")
            continue

        # Normal text
        output.append(_strip_inline(line))

    # Unclosed code block (safety net)
    if in_code and code_lines:
        if fence_lang in _TEXT_FENCE_LABELS:
            clean = [l.strip() for l in code_lines if l.strip()]
            if _is_short_content(clean):
                content = " ".join(clean)
                if content:
                    output.append(content)
            else:
                output.append(_TEXT_PLACEHOLDER)
        else:
            output.append(_CODE_PLACEHOLDER)

    # Trim leading / trailing blank lines
    while output and output[0] == "":
        output.pop(0)
    while output and output[-1] == "":
        output.pop()

    return "\n".join(output)
