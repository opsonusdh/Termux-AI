"""
core/input_handler.py — Enhanced CLI input handler for Termux-AI.

Features:
  - Arrow key navigation (Left/Right/Home/End/Ctrl+A/Ctrl+E).
  - Multiline input support (Alt+Enter, Esc+Enter, backslash continuation).
  - Persistent command history (stored in data/cli_history).
  - Automatic detection and persistence of long pasted content into
    workspace/pasted_content_DDMMYYHHMMSS.txt format.
  - Seamless fallback between prompt_toolkit and readline.
"""
import os
import sys
import time
from datetime import datetime

_CORE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_CORE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import paths
from renderer import GRAY, CYAN, YELLOW, RESET

# Ensure data and workspace directories exist
os.makedirs(paths.DATA_DIR, exist_ok=True)
os.makedirs(paths.WORKSPACE_DIR, exist_ok=True)

HISTORY_FILE = os.path.join(paths.DATA_DIR, "cli_history")
LONG_PASTE_CHAR_THRESHOLD = 300
LONG_PASTE_LINE_THRESHOLD = 4

# Check for prompt_toolkit
_HAS_PROMPT_TOOLKIT = False
_pt_session = None

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.filters import Condition

    _bindings = KeyBindings()

    # Alt+Enter or Esc+Enter inserts newline
    @_bindings.add("escape", "enter")
    def _insert_newline(event):
        event.current_buffer.insert_text("\n")

    # Ctrl+J also inserts newline in many terminals
    @_bindings.add("c-j")
    def _insert_ctrl_j(event):
        event.current_buffer.insert_text("\n")

    _pt_session = PromptSession(
        history=FileHistory(HISTORY_FILE),
        key_bindings=_bindings,
        multiline=False,
    )
    _HAS_PROMPT_TOOLKIT = True
except Exception:
    _HAS_PROMPT_TOOLKIT = False


# Setup fallback readline
_HAS_READLINE = False
if not _HAS_PROMPT_TOOLKIT:
    try:
        import readline
        _HAS_READLINE = True
        try:
            readline.read_history_file(HISTORY_FILE)
        except (FileNotFoundError, IOError):
            pass
        # Standard readline bindings
        readline.parse_and_bind("tab: complete")
        readline.parse_and_bind(r'"\e[A": previous-history')
        readline.parse_and_bind(r'"\e[B": next-history')
        readline.parse_and_bind(r'"\e[C": forward-char')
        readline.parse_and_bind(r'"\e[D": backward-char')
        import atexit
        atexit.register(lambda: readline.write_history_file(HISTORY_FILE))
    except Exception:
        pass


def handle_long_paste(text: str) -> tuple[str, str | None]:
    """
    Detect if text is a large paste (by length or line count).
    If so, save to workspace/pasted_content_DDMMYYHHMMSS.txt and return file info.
    """
    if not text:
        return text, None

    line_count = text.count("\n") + 1
    char_count = len(text)

    if char_count >= LONG_PASTE_CHAR_THRESHOLD or line_count >= LONG_PASTE_LINE_THRESHOLD:
        ts = datetime.now().strftime("%d%m%y%H%M%S")
        filename = f"pasted_content_{ts}.txt"
        filepath = os.path.join(paths.WORKSPACE_DIR, filename)

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(text)

            print(
                f"{GRAY}[Auto-saved pasted content ({char_count} chars, {line_count} lines) "
                f"→ {CYAN}workspace/{filename}{GRAY}]{RESET}"
            )
            annotated_text = f"[Pasted content automatically saved to workspace/{filename}]\n{text}"
            return annotated_text, filepath
        except Exception as e:
            print(f"{YELLOW}[WARN] Could not save pasted content to file: {e}{RESET}")
            return text, None

    return text, None


def get_interactive_input(prompt_str: str = "\nYOU > ") -> str:
    """
    Get interactive input from the user with full arrow-key editing,
    multiline support, persistent history, and automatic paste saving.
    """
    raw_input = ""

    if _HAS_PROMPT_TOOLKIT and _pt_session is not None:
        try:
            # prompt_toolkit supports ANSI escape codes in prompt via ANSI wrapper
            raw_input = _pt_session.prompt(ANSI(prompt_str))
        except (EOFError, KeyboardInterrupt):
            raise
        except Exception:
            # Fallback to standard prompt
            raw_input = input(prompt_str)
    else:
        try:
            first_line = input(prompt_str)
            
            # Check for multiline continuation using backslash '\'
            lines = [first_line]
            while lines and lines[-1].endswith("\\"):
                lines[-1] = lines[-1][:-1]  # remove trailing backslash
                continuation = input(f"{GRAY}... {RESET}")
                lines.append(continuation)

            raw_input = "\n".join(lines)
        except (EOFError, KeyboardInterrupt):
            raise

    cleaned = raw_input.strip()
    if not cleaned:
        return ""

    processed, _ = handle_long_paste(cleaned)
    return processed
