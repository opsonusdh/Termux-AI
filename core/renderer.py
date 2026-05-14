import re


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


# Inline markdown regex
BOLD_RE        = re.compile(r"\*\*(.+?)\*\*")
ITALIC_RE      = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
INLINE_CODE_RE = re.compile(r"`([^`]+)`")


def render_inline(text: str) -> str:
    """
    Render inline markdown formatting.
    Order matters to avoid regex collisions.
    """

    # inline code first
    text = INLINE_CODE_RE.sub(
        lambda m: f"{YELLOW}`{m.group(1)}`{RESET}",
        text
    )

    # bold
    text = BOLD_RE.sub(
        lambda m: f"{BOLD}{m.group(1)}{RESET}",
        text
    )

    # italic
    text = ITALIC_RE.sub(
        lambda m: f"{ITALIC}{m.group(1)}{RESET}",
        text
    )

    return text


def render_markdown_terminal(text: str) -> str:

    lines = text.splitlines()

    rendered = []

    in_code = False
    code_lines = []

    for line in lines:

        stripped = line.strip()

        
        # CODE BLOCK START / END        
        if stripped.startswith("```"):

            # closing
            if in_code:

                width = max(
                    [len(x) for x in code_lines],
                    default=20
                )

                border = "─" * min(max(width, 20), 80)

                rendered.append(f"{GRAY}┌─ code {border}{RESET}")

                for code_line in code_lines:
                    rendered.append(f"{GRAY}{code_line}{RESET}")

                rendered.append(f"{GRAY}└{'─' * (len(border) + 8)}{RESET}")

                code_lines = []
                in_code = False

            # opening
            else:
                in_code = True

            continue

        
        # INSIDE CODE BLOCK        
        if in_code:
            code_lines.append(line.rstrip())
            continue

        
        # DIVIDER        
        if stripped == "---":
            rendered.append(f"{GRAY}{DIVIDER}{RESET}")
            continue

        
        # HEADINGS        
        if stripped.startswith("# "):
            rendered.append(
                f"{BOLD}{BLUE}{render_inline(stripped[2:])}{RESET}"
            )
            continue

        if stripped.startswith("## "):
            rendered.append(
                f"{BOLD}{CYAN}{render_inline(stripped[3:])}{RESET}"
            )
            continue

        if stripped.startswith("### "):
            rendered.append(
                f"{BOLD}{MAG}{render_inline(stripped[4:])}{RESET}"
            )
            continue

        
        # BULLETS        
        if re.match(r"^\s*[-*•]\s+", line):

            bullet_line = re.sub(
                r"^(\s*)[-*•]\s+",
                rf"\1{GREEN}• {RESET}",
                line
            )

            bullet_line = render_inline(bullet_line)

            rendered.append(bullet_line)
            continue

        
        # NUMBERED LISTS        
        if re.match(r"^\s*\d+\.\s+", line):

            numbered = re.sub(
                r"^(\s*)(\d+\.)\s+",
                rf"\1{CYAN}\2{RESET} ",
                line
            )

            numbered = render_inline(numbered)

            rendered.append(numbered)
            continue

        
        # EMPTY LINE        
        if stripped == "":
            rendered.append("")
            continue

        
        # NORMAL TEXT        
        rendered.append(
            render_inline(line)
        )

    # Edge case:
    # unclosed code block
    if in_code and code_lines:

        width = max(
            [len(x) for x in code_lines],
            default=20
        )

        border = "─" * min(max(width, 20), 80)

        rendered.append(f"{GRAY}┌─ code {border}{RESET}")

        for code_line in code_lines:
            rendered.append(f"{GRAY}{code_line}{RESET}")

        rendered.append(f"{GRAY}└{'─' * (len(border) + 8)}{RESET}")

    return "\n".join(rendered)