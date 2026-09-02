"""
core/input_handler.py — Enhanced CLI input handler for Termux-AI.

Features:
  - Arrow key navigation (Left/Right/Home/End/Ctrl+A/Ctrl+E).
  - Multiline input: Enter (or Ctrl+J) submits, Ctrl+N inserts a newline.
  - Persistent command history (stored in data/cli_history).
  - History search with Ctrl+R.
  - Tab completion for commands.
  - Seamless fallback between prompt_toolkit and readline.
"""
import os
import sys
import time

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

    # History search with Ctrl+R
    @_bindings.add("c-r", eager=True)
    def _history_search(event):
        """Ctrl+R for reverse incremental history search."""
        event.current_buffer.start_history_lines_search()

    # Tab completion for commands
    @_bindings.add("tab", eager=True)
    def _tab_complete(event):
        """Tab completion for commands and paths."""
        buffer = event.current_buffer
        text = buffer.text
        cursor_pos = buffer.cursor_position
        
        # Get the word before cursor
        before_cursor = text[:cursor_pos]
        word_start = before_cursor.rfind(' ') + 1
        if word_start > 0 and before_cursor[word_start-1] in ' \t':
            word_start = before_cursor.rfind(' ', 0, word_start-1) + 1
        current_word = before_cursor[word_start:]
        
        # Command completions
        commands = [
            '/delegate', '/agent', '/autonomous', '/chunks', '/clear', '/collapse',
            '/config', '/diagnosis', '/exit', '/expand', '/help', '/index',
            '/recall', '/save_mem', '/project_add', '/project_init', '/project_update',
            '/palette', '/sleep', '/wa_busy', '/wa_chats', '/wa_history',
            '/wa_report', '/wa_search', '/wa_send', '/wa_silence', '/wa_status',
            '/web', '/session_save', '/session_load', '/session_list', '/session_delete',
        ]
        
        # Filter commands that start with current word
        matches = [cmd for cmd in commands if cmd.startswith(current_word)]
        
        if matches:
            if len(matches) == 1:
                # Single match - complete it
                completion = matches[0][len(current_word):]
                buffer.insert_text(completion)
            else:
                # Multiple matches - show common prefix or cycle
                common_prefix = os.path.commonprefix(matches)
                if len(common_prefix) > len(current_word):
                    buffer.insert_text(common_prefix[len(current_word):])
                else:
                    # Cycle through matches
                    if not hasattr(_tab_complete, '_cycle_index'):
                        _tab_complete._cycle_index = 0
                    _tab_complete._cycle_index = (_tab_complete._cycle_index + 1) % len(matches)
                    match = matches[_tab_complete._cycle_index]
                    # Delete current word and insert match
                    buffer.delete_before_cursor(count=len(current_word))
                    buffer.insert_text(match)
        return

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
        # History search with Ctrl+R
        readline.parse_and_bind(r'"\C-r": reverse-search-history')
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


def get_interactive_input(prompt_str: str = "\nYOU > ") -> str:
    """
    Get interactive input from the user with full arrow-key editing,
    multiline support, persistent history, and tab completion.
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

    return cleaned