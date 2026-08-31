"""
core/input_handler.py — Enhanced CLI input handler for Termux-AI.

Features:
  - Arrow key navigation (Left/Right/Home/End/Ctrl+A/Ctrl+E).
  - Multiline input: Enter (or Ctrl+J) submits, Ctrl+N inserts a newline.
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
from core.renderer import GRAY, CYAN, YELLOW, RESET
import core.display_state as display_state

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
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.application import run_in_terminal

    _bindings = KeyBindings()

    @_bindings.add("enter", eager=True)
    def _submit(event):
        """Enter submits the prompt."""
        event.current_buffer.validate_and_handle()

    @_bindings.add("c-n", eager=True)
    def _insert_newline(event):
        """Ctrl+N inserts a newline, for messages spanning multiple lines."""
        event.current_buffer.insert_text("\n")

    @_bindings.add("c-j", eager=True)
    def _submit_alt(event):
        """Ctrl+J also submits (kept as a compatibility alias — harmless
        alongside plain Enter, and useful if a terminal ever mangles Enter)."""
        event.current_buffer.validate_and_handle()

    # Some terminals (kitty keyboard protocol / CSI-u capable ones) send a
    # distinct escape sequence for literal Ctrl+Enter. Bind it opportunistically:
    # if this terminal never emits that sequence, the binding just never fires.
    try:
        @_bindings.add("escape", "[", "1", "3", ";", "5", "u", eager=True)
        def _accept_ctrl_enter_csiu(event):
            event.current_buffer.validate_and_handle()
    except Exception:
        pass

    @_bindings.add("c-o", eager=True)
    def _toggle_details(event):
        """Toggle collapsed/expanded tool & reasoning detail display.

        Presentation-layer only: flips the shared display_state flag that
        llm_client/renderer read when deciding how much detail to print.
        Nothing about tool results or reasoning data is discarded here.
        Also reachable as /expand and /collapse from the chat prompt itself
        (see interface.py), for terminals that don't pass Ctrl+O through.
        """
        expanded = display_state.toggle()
        state = "EXPANDED" if expanded else "COLLAPSED"
        event.app.output.write(f"\r\n[{state} Tool/Reasoning Details]\n")
        event.app.output.flush()

    # Large pastes: intercept the terminal's own bracketed-paste event (the
    # block a terminal sends when you paste, wrapped in ESC[200~/ESC[201~)
    # BEFORE it ever reaches the input buffer, so a large paste never shows
    # up raw in the input line at all — it's saved and replaced with the
    # same short reference handle_long_paste() below already produces for
    # the post-submit path. Small pastes are inserted unchanged. This is
    # also what keeps Enter-submits safe for multiline pastes: bracketed
    # paste content is inserted as literal text, never dispatched through
    # the "enter" binding, so embedded newlines in a paste can't trigger a
    # premature submit either way.
    try:
        @_bindings.add(Keys.BracketedPaste, eager=True)
        def _handle_bracketed_paste(event):
            data = event.data.replace("\r\n", "\n").replace("\r", "\n")

            def _process():
                processed, _ = handle_long_paste(data)
                event.current_buffer.insert_text(processed)

            run_in_terminal(_process)
    except Exception:
        pass

    _pt_session = PromptSession(
        history=FileHistory(HISTORY_FILE),
        key_bindings=_bindings,
        multiline=True,
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


def _fallback_multiline_input(prompt_str: str) -> str:
    """
    Multiline terminal input used when prompt_toolkit is unavailable.

    Enter (or Ctrl+J) submits. Ctrl+N inserts a newline for a multiline
    message — mirrors the prompt_toolkit key bindings above so behavior
    doesn't change if this degraded fallback ever kicks in.
    """
    if os.name == "nt":
        return _fallback_windows_input(prompt_str)
    return _fallback_posix_input(prompt_str)


def _fallback_windows_input(prompt_str: str) -> str:
    import msvcrt

    print(prompt_str, end="", flush=True)
    lines = [""]
    while True:
        ch = msvcrt.getwch()
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x04":
            raise EOFError
        if ch in ("\r", "\n"):  # Enter or Ctrl+J: submit
            break
        if ch == "\x0e":  # Ctrl+N: insert newline
            lines.append("")
            print()
            continue
        if ch == "\b":
            if lines[-1]:
                lines[-1] = lines[-1][:-1]
                print("\b \b", end="", flush=True)
            elif len(lines) > 1:
                lines.pop()
                print("\b \b", end="", flush=True)
            continue
        lines[-1] += ch
        print(ch, end="", flush=True)
    return "\n".join(lines)


def _fallback_posix_input(prompt_str: str) -> str:
    import termios
    import tty

    print(prompt_str, end="", flush=True)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    lines = [""]
    try:
        tty.setraw(fd)
        while True:
            data = os.read(fd, 1)
            if not data:
                raise EOFError
            code = data[0]
            if code == 3:
                raise KeyboardInterrupt
            if code == 4:
                raise EOFError
            if code in (10, 13):  # Ctrl+J (LF) or Enter (CR): submit
                break
            if code == 14:  # Ctrl+N: insert newline
                lines.append("")
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                continue
            if code in (127, 8):
                if lines[-1]:
                    lines[-1] = lines[-1][:-1]
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                elif len(lines) > 1:
                    lines.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            try:
                ch = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            lines[-1] += ch
            sys.stdout.write(ch)
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\r\n")
        sys.stdout.flush()
    return "\n".join(lines)


def handle_long_paste(text: str) -> tuple[str, str | None]:
    """
    Detect if text is a large paste (by length or line count).
    If so, save to workspace/pasted_content_DDMMYYHHMMSS.txt and return file info.
    Also handles the case where text contains multiple newlines that came from
    a single paste event, ensuring they're treated as one cohesive unit.
    
    IMPORTANT: This function is called AFTER paste completion, so we can safely
    process multiline content without triggering premature submission.
    """
    if not text:
        return text, None

    line_count = text.count("\n") + 1
    char_count = len(text)

    if char_count >= LONG_PASTE_CHAR_THRESHOLD or line_count >= LONG_PASTE_LINE_THRESHOLD:
        ts = datetime.now().strftime("%d%m%y-%H%M%S")
        filename = f"pasted_content_{ts}.txt"
        filepath = os.path.join(paths.WORKSPACE_DIR, filename)

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(text)

            print(
                f"{GRAY}[Auto-saved pasted content ({char_count} chars, {line_count} lines) "
                f"→ {CYAN}workspace/{filename}{GRAY}]{RESET}"
            )
            annotated_text = (
                f"[Pasted content saved to workspace/{filename} "
                f"({char_count} chars, {line_count} lines)]"
            )
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
        raw_input = _fallback_multiline_input(prompt_str)

    cleaned = raw_input.strip()
    if not cleaned:
        return ""

    processed, _ = handle_long_paste(cleaned)
    return processed