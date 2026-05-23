import shutil
import re

# Constants for style (assuming these exist in scope)
GRAY = "\033[90m"
RESET = "\033[0m"
BOLD = "\033[1m"
BLUE = "\033[34m"
CYAN = "\033[36m"
MAG = "\033[35m"
GREEN = "\033[32m"
DIVIDER = "─" * 40

def get_visible_len(text: str) -> int:
    """Calculate length excluding ANSI escape codes."""
    return len(re.sub(r'\033\[[0-9;]*m', '', text))

def render_code_block(code_lines, term_width):
    """Render code block formatted to terminal width."""
    # Determine the maximum width needed, capped at screen width
    max_line_len = max((get_visible_len(line) for line in code_lines), default=20)
    
    # We want a clean look: max 80 chars or terminal width, whichever is tighter
    target_width = min(term_width - 4, max(max_line_len, 20))
    
    # Header: ┌─ code ───
    header = f"{GRAY}┌─ code {'─' * (target_width - 5)}{RESET}"
    
    lines = [header]
    for line in code_lines:
        # If line is too long, truncate it or wrap? 
        # For code, truncating or just letting it overflow is standard. 
        # We'll just append it directly for now.
        lines.append(f"{GRAY}{line}{RESET}")
    
    # Footer: └──────────
    footer = f"{GRAY}└{'─' * target_width}{RESET}"
    lines.append(footer)
    return lines

# Re-applying the logic to the file
# Since I cannot modify just the block, I will replace the relevant lines
# in ~ai_root/core/renderer.py
