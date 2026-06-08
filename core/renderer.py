import re
import shutil
import textwrap
from typing import List, Sequence, Tuple

# -----------------------------
# ANSI colour / style codes
# -----------------------------

RESET  = "\033[0m"

BOLD   = "\033[1m"
ITALIC = "\033[3m"
DIM    = "\033[2m"
UNDER  = "\033[4m"

RED    = "\033[31m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
BLUE   = "\033[34m"
MAG    = "\033[35m"
CYAN   = "\033[36m"
GRAY   = "\033[90m"

# Fine-grained attribute reset codes — used instead of RESET to preserve
# parent styles when closing a nested inline span.
COLOR_RESET = "\033[39m"   # Default foreground colour (keeps bold/italic active)
BOLD_OFF    = "\033[22m"   # Normal intensity — turns off bold and dim
ITALIC_OFF  = "\033[23m"   # Italic off
UNDER_OFF   = "\033[24m"   # Underline off
STRIKE_CODE = "\033[9m"    # Crossed-out / strikethrough ON
STRIKE_OFF  = "\033[29m"   # Crossed-out / strikethrough OFF

# -----------------------------
# Regex patterns
# -----------------------------

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[mK]|\x1b\][^\x1b]*(?:\x1b\\|\x07)")

BOLD_RE        = re.compile(r"(?<!\\)\*\*(.+?)\*\*")
ITALIC_RE      = re.compile(r"(?<!\\)(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
INLINE_CODE_RE = re.compile(r"(?<!\\)`([^`]+)`")
STRIKE_RE      = re.compile(r"(?<!\\)~~(.+?)~~")
LINK_RE        = re.compile(r"(?<!\\)\[([^\]]+)\]\(([^)]+)\)")
# Bare https?:// URLs — trailing sentence punctuation excluded from the match.
INLINE_URL_RE  = re.compile(r"https?://[^\s\x00<>\[\]\"']+(?<![.,;:!?])")

TABLE_SEP_CELL_RE = re.compile(r"^\s*:?-+:?\s*$")
DIVIDER_RE        = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")

# Voice renderer regex
_STRIKE_RE_VOICE    = re.compile(r"~~(.+?)~~")
_LINK_RE_VOICE      = re.compile(r"\[([^\]]+)\]\([^\)]*\)")
_BARE_URL_RE        = re.compile(r"https?://\S+")
_TABLE_ROW_RE       = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_ROW_NOPIPE_RE = re.compile(r"^\s*[^|].*[^|]\s*$")

# -----------------------------
# Voice renderer config
# -----------------------------

_VOICE_SHORT_LINES = 3
_VOICE_SHORT_CHARS  = 200

_CODE_PLACEHOLDER = "You can see the code in our conversation history."
_TEXT_PLACEHOLDER = "You can see the text in our conversation history."
_TABLE_PLACEHOLDER = "See the table in our conversation history."

_TEXT_FENCE_LABELS = {"text", "txt", ""}


# ============================================================
# Width helpers
# ============================================================

def _term_size() -> Tuple[int, int]:
    return shutil.get_terminal_size((80, 24))


def visible_len(s: str) -> int:
    """Length after removing ANSI escape sequences."""
    return len(ANSI_ESCAPE.sub("", s))


def display_width(s: str) -> int:
    """
    Best-effort display width.
    Uses wcwidth if available, otherwise falls back to visible_len().
    """
    try:
        from wcwidth import wcswidth  # type: ignore
        width = wcswidth(ANSI_ESCAPE.sub("", s))
        return max(0, width) if width is not None else visible_len(s)
    except Exception:
        return visible_len(s)


def truncate_to_width(text: str, width: int) -> str:
    """Truncate text to a given display width."""
    if width <= 0:
        return ""
    try:
        from wcwidth import wcwidth  # type: ignore
        out = []
        used = 0
        for ch in text:
            w = wcwidth(ch)
            if w is None:
                w = 1
            if used + w > width:
                break
            out.append(ch)
            used += w
        return "".join(out)
    except Exception:
        return text[:width]


def _plain_for_measurement(text: str) -> str:
    """
    Remove markdown markers for width measurement.
    Not a full parser, but enough to keep layout sane.
    """
    text = LINK_RE.sub(r"\1", text)
    text = STRIKE_RE.sub(r"\1", text)
    text = INLINE_CODE_RE.sub(r"\1", text)
    text = BOLD_RE.sub(r"\1", text)
    text = ITALIC_RE.sub(r"\1", text)
    return text


def _visible_measure(text: str) -> int:
    return display_width(text)

def make_divider(term_width: int) -> str:
    term_width = max(1, term_width)

    line_len = max(1, int(term_width * 0.8))
    line_len = min(line_len, term_width)

    padding = max(0, (term_width - line_len) // 2)

    return (
        " " * padding +
        "─" * line_len +
        " " * (term_width - padding - line_len)
    )

# ============================================================
# Inline rendering
# ============================================================

def _stash(pattern: re.Pattern, text: str, formatter, prefix: str = "") -> tuple:
    """Replace all *pattern* matches with opaque placeholder tokens.

    *prefix* disambiguates tokens from different stash passes so they never
    collide when multiple stashes are active simultaneously (e.g. code, link,
    and URL stashes all live in the same text at the same time).
    """
    stash: list[str] = []

    def repl(match):
        token = f"\x00{prefix}{len(stash)}\x00"
        stash.append(formatter(match))
        return token

    return pattern.sub(repl, text), stash


def _restore_stash(text: str, stash: Sequence[str], prefix: str = "") -> str:
    for i, value in enumerate(stash):
        text = text.replace(f"\x00{prefix}{i}\x00", value)
    return text


def _apply_emphasis(text: str) -> str:
    """Resolve bold and italic markers using targeted off-codes instead of
    RESET, so parent styles survive when a nested span closes.

    Correctly handled cases:
        **bold *italic* still bold**    → bold / bold+italic / bold
        *italic **bold** still italic*  → italic / italic+bold / italic
        ***triple*** → bold+italic together
    """
    if not text:
        return text

    # Combined strong+emphasis — must run first so *** isn't split into ** + *
    text = re.sub(
        r"(?<!\\)\*\*\*(.+?)\*\*\*",
        lambda m: f"{BOLD}{ITALIC}{m.group(1)}{ITALIC_OFF}{BOLD_OFF}",
        text,
    )

    # Iterate until stable: each pass resolves one additional nesting level.
    prev = None
    while prev != text:
        prev = text
        text = BOLD_RE.sub(lambda m: f"{BOLD}{m.group(1)}{BOLD_OFF}", text)
        text = ITALIC_RE.sub(lambda m: f"{ITALIC}{m.group(1)}{ITALIC_OFF}", text)

    return text


def _osc8_link(url: str, text: str) -> str:
    """Wrap *text* in an OSC 8 terminal hyperlink pointing at *url*.

    Supported by most modern terminals (kitty, WezTerm, iTerm2, Windows
    Terminal, GNOME Terminal ≥ 3.26, Termux with its VTE backend, etc.).
    Falls back gracefully in terminals that do not support OSC 8 — only the
    plain styled text is shown, without the click handler.

    Wire format:
        ESC ] 8 ; params ; uri  ST  <visible text>  ESC ] 8 ; ;  ST
    where ST is the String Terminator  ESC \\ (\\033\\\\).

    Usage:
        _osc8_link("https://google.com", "\\033[4;34mhttps://google.com\\033[0m")
        _osc8_link("https://example.com", "Example Site")
    """
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def render_inline(text: str) -> str:
    """Apply inline markdown formatting for terminal output.

    Architecture — three-stash pipeline:
      C (code)  — backtick spans, stashed verbatim before any processing.
      L (link)  — ``[display](url)`` turned into an OSC 8 hyperlink.
      U (url)   — bare ``https?://`` URLs turned into OSC 8 hyperlinks.

    Each stash uses a distinct single-letter prefix so their placeholder
    tokens (``\\x00C0\\x00``, ``\\x00L0\\x00``, ``\\x00U0\\x00``, …) never
    collide, fixing the original single-namespace collision bug.

    Nesting — bold / italic close with targeted off-codes (\\033[22m /
    \\033[23m) instead of a global RESET, so a nested span does not kill its
    parent style when it closes:

        **outer *inner* still outer**  →  bold  bold+italic  bold

    Strikethrough uses actual ANSI crossed-out (\\033[9m / \\033[29m) plus
    GRAY colour, avoiding the DIM-vs-bold intensity conflict of the old code.
    """
    if not text:
        return ""

    # ── 1. Code spans — literal; stash before any other processing ──────────
    text, code_stash = _stash(
        INLINE_CODE_RE,
        text,
        lambda m: f"{YELLOW}`{m.group(1)}`{COLOR_RESET}",
        prefix="C",
    )

    # ── 2. Markdown links [display](url) → pretty name + OSC 8 hyperlink ───
    text, link_stash = _stash(
        LINK_RE,
        text,
        lambda m: _osc8_link(
            m.group(2),
            f"{UNDER}{BLUE}{m.group(1)}{UNDER_OFF}{COLOR_RESET}",
        ),
        prefix="L",
    )

    # ── 3. Bare https?:// URLs not already covered by a link stash ──────────
    text, url_stash = _stash(
        INLINE_URL_RE,
        text,
        lambda m: _osc8_link(
            m.group(0),
            f"{UNDER}{BLUE}{m.group(0)}{UNDER_OFF}{COLOR_RESET}",
        ),
        prefix="U",
    )

    # ── 4. Remaining inline styles — targeted off-codes, not global RESET ───
    text = STRIKE_RE.sub(
        lambda m: f"{GRAY}{STRIKE_CODE}{m.group(1)}{STRIKE_OFF}{COLOR_RESET}",
        text,
    )
    text = _apply_emphasis(text)

    # ── 5. Restore all stashes in reverse stash order ───────────────────────
    text = _restore_stash(text, url_stash,  prefix="U")
    text = _restore_stash(text, link_stash, prefix="L")
    text = _restore_stash(text, code_stash, prefix="C")

    return text


# ============================================================
# Table helpers
# ============================================================

def split_md_table_row(line: str) -> List[str]:
    """Split a markdown table row on unescaped pipes."""
    s = line.strip()
    has_outer_pipes = s.startswith("|") and s.endswith("|")
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]

    cells = []
    cur = []
    escaped = False

    for ch in s:
        if escaped:
            cur.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "|":
            cells.append("".join(cur).strip().replace(r"\|", "|"))
            cur = []
        else:
            cur.append(ch)

    cells.append("".join(cur).strip().replace(r"\|", "|"))

    # For no-outer-pipe tables, preserve the split only if it actually looks tabular.
    if not has_outer_pipes and len(cells) < 2:
        return [line.rstrip()]

    return cells


def is_table_separator_row(cells: Sequence[str]) -> bool:
    return len(cells) >= 2 and all(TABLE_SEP_CELL_RE.match(cell or "") for cell in cells)


def _is_table_block_start(lines: Sequence[str], i: int) -> bool:
    """
    Detect:
    1) standard pipe tables
    2) no-outer-pipe tables

    Requires a separator row directly after the header row.
    """
    if i + 1 >= len(lines):
        return False

    row1 = split_md_table_row(lines[i])
    row2 = split_md_table_row(lines[i + 1])

    if not is_table_separator_row(row2):
        return False

    return len(row1) >= 2 and len(row1) == len(row2)


def _table_row_looks_valid(row: Sequence[str]) -> bool:
    return len(row) >= 2


def _wrap_plain_by_width(text: str, width: int) -> List[str]:
    """
    Wrap plain text by display width.
    This is used for tables after markdown markers have been stripped for layout.
    """
    width = max(1, width)
    if not text:
        return [""]

    words = text.split()
    if not words:
        return [""]

    lines: List[str] = []
    cur = ""

    def cur_w(s: str) -> int:
        return display_width(s)

    for word in words:
        candidate = word if not cur else f"{cur} {word}"
        if cur_w(candidate) <= width:
            cur = candidate
            continue

        if cur:
            lines.append(cur)
            cur = ""

        # Word itself too long, break hard by display width.
        if cur_w(word) <= width:
            cur = word
        else:
            chunk = ""
            for ch in word:
                if cur_w(chunk + ch) <= width:
                    chunk += ch
                else:
                    if chunk:
                        lines.append(chunk)
                    chunk = ch
            cur = chunk

    if cur:
        lines.append(cur)

    return lines or [""]


def pad_ansi_string(s: str, width: int, align: str = "left") -> str:
    """Pad a string containing ANSI escape codes to a visible width."""
    vis_len = display_width(s)
    if vis_len >= width:
        return s

    needed = width - vis_len
    if align == "center":
        left = needed // 2
        right = needed - left
        return " " * left + s + " " * right
    if align == "right":
        return " " * needed + s
    return s + " " * needed


def make_border(col_widths: Sequence[int], left: str, mid: str, right: str, line_char: str = "─") -> str:
    parts = [line_char * (w + 2) for w in col_widths]
    return f"{left}{mid.join(parts)}{right}"


def fit_column_widths(max_widths: Sequence[int], term_width: int, min_col_width: int = 3) -> List[int]:
    """
    Fit columns into terminal width.
    Keeps things sane in narrow terminals instead of pretending every terminal is a cathedral.
    """
    num_cols = len(max_widths)
    if num_cols == 0:
        return []

    overhead = 3 * num_cols + 1  # borders + padding + separators
    available = max(1, term_width - overhead)

    widths = [max(min_col_width, w) for w in max_widths]
    total = sum(widths)

    if total > available:
        excess = total - available
        while excess > 0:
            shrinkable = [i for i, w in enumerate(widths) if w > min_col_width]
            if not shrinkable:
                break
            i = max(shrinkable, key=lambda idx: widths[idx])
            widths[i] -= 1
            excess -= 1
    elif total < available:
        extra = available - total
        i = 0
        while extra > 0 and num_cols > 0:
            widths[i % num_cols] += 1
            extra -= 1
            i += 1

    return widths


def _render_code_block(code_lines: Sequence[str], lang_str: str, term_width: int) -> List[str]:
    """
    Render a fenced code block with a consistent box.
    Border width is fixed from one calculation so top and bottom always match.
    """
    clean_lang = (lang_str or "code").strip()
    max_line_width = max((display_width(x) for x in code_lines), default=20)

    # Keep the label visible, but don't let it push the box wider than the screen.
    max_box_width = max(10, term_width - 4)
    label = clean_lang
    label_room = max(0, max_box_width - 3)  # ┌ + ┐ consume 2, want at least one dash/space
    if display_width(label) > max(0, label_room - 4):
        label = truncate_to_width(label, max(0, label_room - 4))

    inner_width = max(
        20,
        max_line_width + 2,
        display_width(label) + 4,
    )
    inner_width = min(inner_width, max_box_width)

    # Top border: corners + one dash + label + one dash + fill dashes
    left_dash = 1
    label_fragment = f" {label} "
    right_dash = max(1, inner_width - left_dash - display_width(label_fragment))
    top = f"{GRAY}┌{'─' * left_dash}{label_fragment}{'─' * right_dash}┐{RESET}"
    bottom = f"{GRAY}└{'─' * inner_width}┘{RESET}"

    rendered = [top]
    for code_line in code_lines:
        rendered.append(f"{GRAY}{code_line}{RESET}")
    rendered.append(bottom)
    return rendered


def render_table(raw_rows: List[List[str]], term_width: int, header_color: str = BOLD + CYAN, border_color: str = GRAY) -> str:
    """Render a markdown table cleanly in the terminal."""
    if not raw_rows:
        return ""

    num_cols = max(len(row) for row in raw_rows)
    if num_cols == 0:
        return ""

    rows = [row[:] + [""] * (num_cols - len(row)) for row in raw_rows]

    has_header = False
    alignments = ["left"] * num_cols

    if len(rows) >= 2 and is_table_separator_row(rows[1]) and len(rows[0]) == len(rows[1]):
        has_header = True
        for i, cell in enumerate(rows[1]):
            c = cell.strip()
            if c.startswith(":") and c.endswith(":"):
                alignments[i] = "center"
            elif c.endswith(":"):
                alignments[i] = "right"
            else:
                alignments[i] = "left"

    headers = rows[0] if has_header else None
    data_rows = rows[2:] if has_header else rows

    # Measure visible content width.
    max_widths = [0] * num_cols
    content_rows = data_rows + ([headers] if headers else [])
    for row in content_rows:
        for i, cell in enumerate(row):
            plain = _plain_for_measurement(cell)
            cell_width = max((display_width(part) for part in plain.splitlines()), default=0)
            max_widths[i] = max(max_widths[i], cell_width)

    # Tiny terminal fallback: stacked layout.
    overhead = 3 * num_cols + 1
    if term_width < 30 or term_width - overhead < num_cols * 3:
        lines = []
        if headers:
            for r_idx, row in enumerate(data_rows):
                if r_idx > 0:
                    lines.append(f"{border_color}{'-' * max(5, min(term_width, 20))}{RESET}")
                for c_idx, cell in enumerate(row):
                    label = render_inline(headers[c_idx])
                    value = render_inline(cell)
                    lines.append(f"{header_color}{label}{RESET}: {value}")
        else:
            for row in data_rows:
                for c_idx, cell in enumerate(row):
                    lines.append(f"{CYAN}{c_idx + 1}.{RESET} {render_inline(cell)}")
                lines.append("")
        return "\n".join(line.rstrip() for line in lines).rstrip()

    col_widths = fit_column_widths(max_widths, term_width, min_col_width=3)
    output = [f"{border_color}{make_border(col_widths, '┌', '┬', '┐')}{RESET}"]

    def format_row(cells: Sequence[str], is_header: bool = False) -> str:
        # Render each cell after measuring/wrapping by plain text.
        wrapped_cells: List[List[str]] = []
        for cell, width in zip(cells, col_widths):
            plain = _plain_for_measurement(cell)
            wrapped_plain = _wrap_plain_by_width(plain, width)
            wrapped_cells.append(wrapped_plain)

        line_count = max((len(w) for w in wrapped_cells), default=1)
        row_lines = []

        for line_idx in range(line_count):
            parts = []
            for col_idx, width in enumerate(col_widths):
                cell_lines = wrapped_cells[col_idx]
                raw_text = cell_lines[line_idx] if line_idx < len(cell_lines) else ""
                formatted = render_inline(raw_text)
                if is_header:
                    formatted = f"{header_color}{formatted}{RESET}"
                padded = pad_ansi_string(formatted, width, alignments[col_idx])
                parts.append(f" {padded} ")

            sep = f"{border_color}│{RESET}"
            row_lines.append(f"{border_color}│{RESET}{sep.join(parts)}{border_color}│{RESET}")

        return "\n".join(row_lines)

    if headers is not None:
        output.append(format_row(headers, is_header=True))
        output.append(f"{border_color}{make_border(col_widths, '├', '┼', '┤')}{RESET}")

    for row in data_rows:
        output.append(format_row(row, is_header=False))

    output.append(f"{border_color}{make_border(col_widths, '└', '┴', '┘')}{RESET}")
    return "\n".join(output)


# ============================================================
# Markdown terminal renderer
# ============================================================

# Matches a leading emoji/symbol cluster and/or a common numbering token
# (1.  I.  a.  (1)  [I]  I]  etc.) at the very start of a heading body.
# Used by _split_heading_prefix to separate undecorated prefix from the
# part of the heading that should receive the underline.
_HEADING_PREFIX_RE = re.compile(
    r"^("
    # ① Optional emoji / special-symbol cluster (absorbs trailing whitespace)
    r"(?:["
    r"\U0001F300-\U0001FAFF"   # misc symbols & pictographs, emoticons
    r"\U0001F1E6-\U0001F1FF"   # regional indicators
    r"\U00002600-\U000026FF"   # misc symbols
    r"\U00002700-\U000027BF"   # dingbats
    r"\u2B50\u2605\u2606"      # ⭐ ★ ☆
    r"\ufe0f\u200d"            # variation selector-16 / ZWJ
    r"]+\s*)?"                 # cluster optional; \s* absorbs trailing space
    # ② Optional numbering token + mandatory gap
    #    \s+ is placed AFTER a wrapper group so it applies to every alternative,
    #    not just the last one.
    r"(?:"                     # outer nc — the whole ② group
    r"(?:"                     # inner nc — one of the token shapes
    r"\(\d{1,4}\)"             # (1) … (9999)
    r"|\[[A-Za-z0-9]{1,5}\]"  # [I]  [ii]  [1]  [A2]
    r"|[A-Za-z0-9]{1,5}\]"    # I]   ii]   1]   (at most 5 chars before ])
    r"|(?:\d{1,4}"             # digits:  1.  12.  1)
    r"|[IVXLCDMivxlcdm]{1,8}" # Roman:   I.  IV.  xii.
    r"|[a-zA-Z]{1,2}"         # letters: a.  A.   ab.
    r")[.)]"
    r")"                       # close inner nc (token shapes)
    r"\s+)?"                   # mandatory gap after ANY token; outer nc optional
    r")",                      # close outer capturing group 1
    re.UNICODE,
)


def _split_heading_prefix(text: str) -> tuple:
    """Split a heading body into ``(prefix, body)``.

    *prefix* — any leading emoji cluster and/or numbering token
               (``1.``, ``I.``, ``a.``, ``(1)``, ``[I]``, ``I]``, …).
               Receives bold + colour but **no underline**.
    *body*   — the actual heading words that receive the underline.

    Returns ``("", text)`` when no recognisable prefix is found.
    """
    m = _HEADING_PREFIX_RE.match(text)
    if m:
        prefix = m.group(1) or ""
        body = text[len(prefix):]
        if prefix and body:      # need a non-empty body to split
            return prefix, body
    return "", text

def render_markdown_terminal(text: str) -> str:
    """Transform markdown into ANSI-coloured terminal output."""
    term_width, _ = _term_size()

    lines = text.splitlines()
    rendered: List[str] = []
    in_code = False
    code_lines: List[str] = []
    code_lang = "code"

    i = 0
    num_lines = len(lines)

    while i < num_lines:
        line = lines[i]
        stripped = line.strip()

        # Code block open / close
        if stripped.startswith("```"):
            if in_code:
                rendered.extend(_render_code_block(code_lines, code_lang, term_width))
                code_lines = []
                in_code = False
                code_lang = "code"
            else:
                in_code = True
                content = stripped[3:].strip()
                code_lang = content.split()[0] if content else "code"
            i += 1
            continue

        if in_code:
            code_lines.append(line.rstrip())
            i += 1
            continue

        # Table block
        if _is_table_block_start(lines, i):
            table_lines: List[str] = []
            j = i
            while j < num_lines and re.search(r"(?<!\\)\|", lines[j]):
                table_lines.append(lines[j])
                j += 1

            raw_rows = [split_md_table_row(t_line) for t_line in table_lines]
            rendered_table = render_table(raw_rows, term_width)
            rendered.extend(rendered_table.splitlines())
            i = j
            continue

        # Divider
        if DIVIDER_RE.match(stripped) or stripped in ("---", "***", "___", "- - -", "* * *", "_ _ _"):
            rendered.append(f"{GRAY}{make_divider(term_width)}{RESET}")
            i += 1
            continue

        # Headings — prefix (emoji / numbering) gets bold+colour only;
        # the body text alone is underlined.
        if stripped.startswith("# "):
            pfx, body = _split_heading_prefix(stripped[2:])
            rendered.append(f"{BOLD}{BLUE}{pfx}{UNDER}{render_inline(body)}{UNDER_OFF}{RESET}")
            i += 1
            continue
        if stripped.startswith("## "):
            pfx, body = _split_heading_prefix(stripped[3:])
            rendered.append(f"{BOLD}{CYAN}{pfx}{UNDER}{render_inline(body)}{UNDER_OFF}{RESET}")
            i += 1
            continue
        if stripped.startswith("### "):
            pfx, body = _split_heading_prefix(stripped[4:])
            rendered.append(f"{BOLD}{MAG}{pfx}{UNDER}{render_inline(body)}{UNDER_OFF}{RESET}")
            i += 1
            continue
        if stripped.startswith("#### "):
            pfx, body = _split_heading_prefix(stripped[5:])
            rendered.append(f"{BOLD}{GREEN}{pfx}{UNDER}{render_inline(body)}{UNDER_OFF}{RESET}")
            i += 1
            continue
        if stripped.startswith("##### "):
            pfx, body = _split_heading_prefix(stripped[6:])
            rendered.append(f"{BOLD}{YELLOW}{pfx}{UNDER}{render_inline(body)}{UNDER_OFF}{RESET}")
            i += 1
            continue
        if stripped.startswith("###### "):
            pfx, body = _split_heading_prefix(stripped[7:])
            rendered.append(f"{BOLD}{pfx}{UNDER}{render_inline(body)}{UNDER_OFF}{RESET}")
            i += 1
            continue

        # Blockquote
        if stripped.startswith(">"):
            rendered.append(f"{DIM}│ {render_inline(stripped[1:].lstrip())}{RESET}")
            i += 1
            continue

        # Bullet lists
        if re.match(r"^\s*[-*•]\s+", line):
            bullet_line = re.sub(r"^(\s*)[-*•]\s+", rf"\1{GREEN}• {RESET}", line)
            rendered.append(render_inline(bullet_line))
            i += 1
            continue

        # Numbered lists
        if re.match(r"^\s*\d+\.\s+", line):
            numbered = re.sub(r"^(\s*)(\d+\.)\s+", rf"\1{CYAN}\2{RESET} ", line)
            rendered.append(render_inline(numbered))
            i += 1
            continue

        # Empty line
        if stripped == "":
            rendered.append("")
            i += 1
            continue

        # Normal text
        rendered.append(render_inline(line))
        i += 1

    # Unclosed code block safety net
    if in_code and code_lines:
        rendered.extend(_render_code_block(code_lines, code_lang, term_width))

    return "\n".join(rendered)


render_for_printing = render_markdown_terminal

# ============================================================
# Voice / TTS sanitiser
# ============================================================

def _is_short_content(lines: List[str]) -> bool:
    joined = " ".join(lines).strip()
    return len(lines) <= _VOICE_SHORT_LINES and len(joined) <= _VOICE_SHORT_CHARS


def _shorten_url(url: str) -> str:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc
        return host if host else url
    except Exception:
        return url


def _strip_inline(text: str) -> str:
    """
    Strip markdown markers into speakable text.
    """
    text = _LINK_RE_VOICE.sub(r"\1", text)
    text = _BARE_URL_RE.sub(lambda m: _shorten_url(m.group(0)), text)
    text = _STRIKE_RE_VOICE.sub(r"\1", text)
    text = INLINE_CODE_RE.sub(r"\1", text)
    text = BOLD_RE.sub(r"\1", text)
    text = ITALIC_RE.sub(r"\1", text)
    return text


def _is_table_row(line: str) -> bool:
    s = line.rstrip()
    return bool(_TABLE_ROW_RE.match(s) or ("|" in s and _TABLE_ROW_NOPIPE_RE.match(s)))


def _preprocess_tables(text: str) -> str:
    """
    Replace markdown tables with a short spoken placeholder before line-by-line processing.
    Uses the same broad table detection as the terminal renderer.
    """
    lines = text.splitlines(keepends=True)
    result = []
    buf = []

    def flush():
        if len(buf) >= 2:
            result.append(_TABLE_PLACEHOLDER + "\n")
        else:
            result.extend(buf)
        buf.clear()

    for line in lines:
        if _is_table_row(line):
            buf.append(line)
        else:
            if buf:
                flush()
            result.append(line)

    if buf:
        flush()

    return "".join(result)


def render_for_voice(text: str) -> str:
    """Sanitise markdown for voice / TTS output."""
    text = _preprocess_tables(text)

    # Leading emoji / symbol clusters often appear in generated headings.
    # Remove them only at the start of heading content, not everywhere.
    LEADING_EMOJI_RE = re.compile(
        r"^(?:[\s"
        r"\U0001F300-\U0001FAFF"  # misc emoji blocks
        r"\U0001F1E6-\U0001F1FF"  # regional indicators
        r"\U00002600-\U000026FF"  # misc symbols
        r"\U00002700-\U000027BF"  # dingbats
        r"\ufe0f"                 # variation selector
        r"\u200d"                 # zero width joiner
        r"]+)"
    )

    def strip_leading_heading_emoji(s: str) -> str:
        s = LEADING_EMOJI_RE.sub("", s)
        return s.lstrip(" -–—:|•*#")

    lines = text.splitlines()
    output: List[str] = []
    in_code = False
    fence_lang = ""
    code_lines: List[str] = []

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code:
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

                code_lines = []
                fence_lang = ""
                in_code = False
            else:
                content = stripped[3:].strip()
                fence_lang = content.lower().split()[0] if content else ""
                in_code = True
            continue

        if in_code:
            code_lines.append(line.rstrip())
            continue

        if DIVIDER_RE.match(stripped):
            continue

        if stripped == "":
            if output and output[-1] != "":
                output.append("")
            continue

        m = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if m:
            level = len(m.group(1))
            content_val = m.group(2)
            clean_content = strip_leading_heading_emoji(_strip_inline(content_val))
            if level == 1:
                clean_content = clean_content.upper()
            output.append(clean_content)
            continue

        if stripped.startswith(">"):
            output.append(_strip_inline(stripped[1:].lstrip()))
            continue

        m = re.match(r"^(\s*)[-*•]\s+(.+)$", line)
        if m:
            output.append(_strip_inline(m.group(2)))
            continue

        m = re.match(r"^\s*(\d+)\.\s+(.+)$", line)
        if m:
            output.append(f"{m.group(1)}. {_strip_inline(m.group(2))}")
            continue

        output.append(_strip_inline(line))

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

    while output and output[0] == "":
        output.pop(0)
    while output and output[-1] == "":
        output.pop()

    return "\n".join(output)
