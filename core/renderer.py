import re
import shutil
import textwrap

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


# TABLE RENDERING HELPERS

ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[mK]')

def visible_len(s: str) -> int:
    """Return the length of a string minus ANSI escape sequences."""
    return len(ANSI_ESCAPE.sub('', s))

def wrap_cell(text: str, width: int) -> list:
    """Wrap cell text to a given width, breaking lines if necessary."""
    if not text:
        return [""]
    lines = []
    for line in text.splitlines():
        wrapped = textwrap.wrap(line, width=width, break_long_words=True, break_on_hyphens=False)
        if not wrapped:
            lines.append("")
        else:
            lines.extend(wrapped)
    return lines

def pad_ansi_string(s: str, width: int, align: str = 'left') -> str:
    """Pad a string containing ANSI escape codes to a visible width."""
    vis_len = visible_len(s)
    if vis_len >= width:
        return s
    needed = width - vis_len
    if align == 'center':
        left = needed // 2
        right = needed - left
        return " " * left + s + " " * right
    elif align == 'right':
        return " " * needed + s
    else:
        return s + " " * needed

def make_border(col_widths: list, left: str, mid: str, right: str, line_char: str = '─') -> str:
    """Create a unicode box-drawing horizontal border."""
    parts = []
    for w in col_widths:
        parts.append(line_char * (w + 2))  # +2 for padding space on both sides
    return f"{left}{mid.join(parts)}{right}"

def render_table(raw_rows: list, term_width: int, header_color: str = BOLD+CYAN, border_color: str = GRAY) -> str:
    """Render a table prettily based on terminal width."""
    num_cols = max(len(row) for row in raw_rows)
    if num_cols == 0:
        return ""
        
    for row in raw_rows:
        while len(row) < num_cols:
            row.append("")
            
    # Check if there is a separator row at index 1
    has_separator = False
    alignments = ['left'] * num_cols
    
    if len(raw_rows) >= 2:
        second_row = raw_rows[1]
        if all(re.match(r"^\s*:?-+:?\s*$", cell) for cell in second_row):
            has_separator = True
            # Parse alignments
            for col_idx, cell in enumerate(second_row):
                if col_idx >= num_cols:
                    break
                c = cell.strip()
                if c.startswith(':') and c.endswith(':'):
                    alignments[col_idx] = 'center'
                elif c.startswith(':'):
                    alignments[col_idx] = 'left'
                elif c.endswith(':'):
                    alignments[col_idx] = 'right'
                else:
                    alignments[col_idx] = 'left'
                    
    # Separate headers, separator, and data rows
    if has_separator:
        headers = raw_rows[0]
        data_rows = raw_rows[2:]
    else:
        headers = None
        data_rows = raw_rows
        
    # Calculate column width requirements (based on cell content)
    max_widths = [0] * num_cols
    all_content_rows = data_rows + ([headers] if headers else [])
    for row in all_content_rows:
        for col_idx, cell in enumerate(row):
            max_widths[col_idx] = max(max_widths[col_idx], len(cell))
            
    # Calculate terminal overhead:
    # │ col1 │ col2 │ -> 1 left, 1 right, (num_cols-1) dividers, and 2 spaces padding per column.
    overhead = 1 + 1 + (num_cols - 1) + 2 * num_cols
    available_text_width = term_width - overhead
    
    # Distribute column widths
    if available_text_width <= 0:
        col_widths = [max(1, w) for w in max_widths]
    else:
        min_col_width = min(5, max(1, available_text_width // num_cols))
        col_widths = [min(max_widths[i], min_col_width) for i in range(num_cols)]
        remaining_width = available_text_width - sum(col_widths)
        
        if remaining_width > 0:
            active_cols = [i for i in range(num_cols) if col_widths[i] < max_widths[i]]
            while remaining_width > 0 and active_cols:
                sum_active_max = sum(max_widths[i] for i in active_cols)
                if sum_active_max == 0:
                    break
                for i in list(active_cols):
                    needed = max_widths[i] - col_widths[i]
                    share = int(remaining_width * (max_widths[i] / sum_active_max))
                    to_add = min(needed, share, remaining_width)
                    if to_add == 0 and remaining_width > 0:
                        to_add = 1
                    col_widths[i] += to_add
                    remaining_width -= to_add
                    if col_widths[i] == max_widths[i]:
                        active_cols.remove(i)
                    if remaining_width <= 0:
                        break
                        
    # Now build the table strings
    output = []
    
    # Top border
    output.append(f"{border_color}{make_border(col_widths, '┌', '┬', '┐')}{RESET}")
    
    # Helper to render a single row of cells (possibly multi-line due to wrapping)
    def format_row(cells, is_header=False):
        wrapped_cells = [wrap_cell(cell, w) for cell, w in zip(cells, col_widths)]
        num_lines = max(len(w) for w in wrapped_cells)
        row_lines = []
        for line_idx in range(num_lines):
            line_parts = []
            for col_idx, width in enumerate(col_widths):
                cell_lines = wrapped_cells[col_idx]
                raw_text = cell_lines[line_idx] if line_idx < len(cell_lines) else ""
                
                # Apply inline formatting
                formatted_text = render_inline(raw_text)
                if is_header:
                    formatted_text = f"{header_color}{formatted_text}{RESET}"
                
                # Pad
                align = alignments[col_idx]
                padded = pad_ansi_string(formatted_text, width, align)
                line_parts.append(f" {padded} ")
            
            col_sep = f"{border_color}│{RESET}"
            row_lines.append(f"{border_color}│{RESET}{col_sep.join(line_parts)}{border_color}│{RESET}")
        return "\n".join(row_lines)
        
    # Render header
    if headers:
        output.append(format_row(headers, is_header=True))
        output.append(f"{border_color}{make_border(col_widths, '├', '┼', '┤')}{RESET}")
        
    # Render data rows
    for row in data_rows:
        output.append(format_row(row, is_header=False))
        
    # Bottom border
    output.append(f"{border_color}{make_border(col_widths, '└', '┴', '┘')}{RESET}")
    
    return "\n".join(output)


def render_markdown_terminal(text: str) -> str:
    """Transform markdown into ANSI-coloured terminal output."""
    term_width, _ = shutil.get_terminal_size((80, 24))

    lines    = text.splitlines()
    rendered = []
    in_code  = False
    code_lines = []
    code_lang = "code"

    i = 0
    num_lines = len(lines)
    while i < num_lines:
        line = lines[i]
        stripped = line.strip()

        # Code block open / close
        if stripped.startswith("```"):
            if in_code:
                # Optimized block
                max_width = max((len(x) for x in code_lines), default=20)
                lang_str = code_lang
                
                min_width = max(20, len(lang_str) + 4)
                target_width = min(term_width - 4, max(max_width, min_width))
                target_width = max(target_width, len(lang_str) + 4)
                
                dash_count = target_width - len(lang_str) - 3
                rendered.append(f"{GRAY}┌─ {lang_str} {'─' * dash_count}{RESET}")
                for code_line in code_lines:
                    rendered.append(f"{GRAY}{code_line}{RESET}")
                rendered.append(f"{GRAY}└{'─' * (target_width + 2)}{RESET}")
                code_lines = []
                in_code    = False
            else:
                in_code = True
                try:
                    # Capture language tag: ```bash -> bash, ``` bash -> bash
                    content = stripped[3:].strip()
                    code_lang = content if content else "code"
                except Exception:
                    code_lang = "code"
            i += 1
            continue

        # Inside code block
        if in_code:
            code_lines.append(line.rstrip())
            i += 1
            continue

        # Check for table block (only when not in code block)
        if re.match(r"^\s*\|.*\|\s*$", line):
            # Collect all consecutive table lines
            table_lines = []
            j = i
            while j < num_lines and re.match(r"^\s*\|.*\|\s*$", lines[j]):
                table_lines.append(lines[j])
                j += 1
                
            if len(table_lines) >= 1:
                raw_rows = []
                for t_line in table_lines:
                    s = t_line.strip()
                    if s.startswith('|'):
                        s = s[1:]
                    if s.endswith('|'):
                        s = s[:-1]
                    cells = [cell.strip().replace(r'\|', '|') for cell in re.split(r'(?<!\\)\|', s)]
                    raw_rows.append(cells)
                
                rendered_table = render_table(raw_rows, term_width)
                for r_line in rendered_table.splitlines():
                    rendered.append(r_line)
                    
                i = j
                continue

        # Divider
        if stripped == "---":
            rendered.append(f"{GRAY}{DIVIDER}{RESET}")
            i += 1
            continue

        # Headings
        if stripped.startswith("# "):
            rendered.append(f"{BOLD}{BLUE}{render_inline(stripped[2:])}{RESET}")
            i += 1
            continue
        if stripped.startswith("## "):
            rendered.append(f"{BOLD}{CYAN}{render_inline(stripped[3:])}{RESET}")
            i += 1
            continue
        if stripped.startswith("### "):
            rendered.append(f"{BOLD}{MAG}{render_inline(stripped[4:])}{RESET}")
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

    # Unclosed code block (safety net)
    if in_code and code_lines:
        max_width = max((len(x) for x in code_lines), default=20)
        lang_str = code_lang if ('code_lang' in locals() and code_lang) else "code"
        
        min_width = max(20, len(lang_str) + 4)
        target_width = min(term_width - 4, max(max_width, min_width))
        target_width = max(target_width, len(lang_str) + 4)
        
        dash_count = target_width - len(lang_str) - 3
        rendered.append(f"{GRAY}┌─ {lang_str} {'─' * dash_count}{RESET}")
        for code_line in code_lines:
            rendered.append(f"{GRAY}{code_line}{RESET}")
        rendered.append(f"{GRAY}└{'─' * (target_width + 2)}{RESET}")

    return "\n".join(rendered)

# Alias for user-friendly script capability
render_for_printing = render_markdown_terminal


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
                content = stripped[3:].strip()
                fence_lang = content.lower() if content else ""
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
