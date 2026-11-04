import os
import json
import sys
import subprocess
from concurrent.futures import ThreadPoolExecutor

# Path bootstrap
_CORE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_CORE)
sys.dont_write_bytecode = True
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)
if _ROOT not in sys.path:
    sys.path.insert(1, _ROOT)

import paths

# Project imports
from llm_client import ask_ai, ask_agent, get_last_response_metadata
from core.renderer import render_markdown_terminal, GRAY, RESET, RED, YELLOW, GREEN, BLUE, CYAN
from core.input_handler import get_interactive_input
import core.display_state as display_state
import core.runtime_log as runtime_log
import core.ui as ui
import core.app_config as app_config
import core.notifier as notifier
from core.tools import *
import context_manager as _cm
from core.plugin_manager import plugin_manager

# The WhatsApp manager is a flat module that only imports when the Termux-WP
# checkout is present.  ``from tools import *`` above binds the name only when
# that import succeeded, so relying on it left this name undefined on a
# device without Termux-WP — and the startup path below turned that into a
# NameError instead of a message.
try:
    from whatsapp_manager import whatsapp_manager
except (ImportError, FileNotFoundError):
    whatsapp_manager = None

# Session memory
try:
    from core.session_memory import (
        list_sessions,
        resume_session,
        delete_session,
        finalize_session,
        get_session_draft,
    )
except Exception:
    list_sessions = resume_session = delete_session = finalize_session = get_session_draft = None

# Config
BASE_DIR    = _ROOT
CONFIG_PATH = paths.CONFIG_FILE
# The canonical default set lives in core/app_config.py.  Three modules used
# to carry their own copy of it and they drifted — the copies in
# core/tools.py and core/permissions.py never learned about the "notify"
# flag, so a config.json they created was missing a key this interface
# expected.
DEFAULT_CONFIG = dict(app_config.DEFAULT_CONFIG)
os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
if not os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=4)
        
try:
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
except:
    config = DEFAULT_CONFIG

# Ensure all required config keys exist
for key, value in DEFAULT_CONFIG.items():
    if key not in config:
        config[key] = value

# The STT checkout location comes from config.json.  A hardcoded default used
# to be re-applied further down this module, which silently ignored whatever
# the user had configured — the setting looked editable but did nothing.
STT_PATH = os.path.expanduser(config["stt_path"])
if STT_PATH not in sys.path:
    sys.path.insert(0, STT_PATH)


def _send_notification(title: str, message: str) -> None:
    """Send a system notification using termux-notification.

    Routed through ``core.notifier`` so that the ``notify`` flag in
    config.json is honoured in exactly one place: the same flag that decides
    whether the runtime pings the user for a permission request or a question
    decides this one too.  A missing Termux:API package is not an error —
    the notification is simply skipped.
    """
    notifier.push(title, message)


def _update_config(**kwargs) -> dict:
    """Merge kwargs into config.json with a fresh read right before writing.

    display_state.py (Ctrl+O / /expand / /collapse) persists to this same
    file independently and can run mid-session — e.g. while a message is
    still being composed, before this loop's next top-of-loop reload. Reading
    fresh here (instead of writing back whatever this module's `config` var
    last held) avoids clobbering a show_details change that landed after
    `config` was last loaded but before this write.
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = dict(config)
    data.update(kwargs)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp, CONFIG_PATH)
    return data


def _read_config() -> dict:
    """The stored configuration, read fresh from disk.

    The module-level ``config`` is a snapshot taken at import time.  The
    notifier reads ``config.json`` at every prompt, so a status command has
    to read the same file — otherwise the two can disagree (e.g. the flag was
    edited by hand while the session was running) and the command reports a
    state the runtime is not actually in.
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = dict(config)
    return data


def _handle_notify_command(command_low: str) -> bool:
    """Handle a ``/notify`` command.  Returns True when it was consumed.

    The command used to switch the flag silently — no output at all, so
    "/notify true" looked like it had been ignored.  It now confirms what it
    did, reports the current state on request, and proves the plumbing works
    by sending one test notification when notifications are switched on.
    """
    global config

    if command_low in ("/notify true", "/notify on", "/notify enable",
                       "notify true", "notify on"):
        config = _update_config(notify=True)
        print(ui.notice("ok", "Notifications: ON — you will be pinged when "
                              "the AI needs your permission, a confirmation "
                              "or an answer."))
        _send_notification("Termux-AI: Notifications enabled",
                           "Notifications are on. The AI will ping you when "
                           "it needs a permission, a confirmation or an "
                           "answer.")
        return True

    if command_low in ("/notify false", "/notify off", "/notify disable",
                       "notify false", "notify off"):
        config = _update_config(notify=False)
        print(ui.notice("muted", "Notifications: OFF — the AI will no "
                                "longer ping you while it waits."))
        return True

    if command_low in ("/notify toggle", "notify toggle"):
        # Read the current value from disk, not from the import-time
        # snapshot, so the toggle reflects what is really in force.
        new_state = not bool(_read_config().get("notify", True))
        config = _update_config(notify=new_state)
        if new_state:
            print(ui.notice("ok", "Notifications: ON — you will be pinged "
                                  "when the AI needs your permission, a "
                                  "confirmation or an answer."))
            _send_notification("Termux-AI: Notifications enabled",
                               "Notifications are on. The AI will ping you "
                               "when it needs a permission, a confirmation or "
                               "an answer.")
        else:
            print(ui.notice("muted", "Notifications: OFF — the AI will no "
                                    "longer ping you while it waits."))
        return True

    if command_low in ("/notify", "/notify status", "notify",
                       "notify status"):
        on = bool(_read_config().get("notify", True))
        print(ui.notice("ok" if on else "muted",
                        f"Notifications: {'ON' if on else 'OFF'}"))
        if on:
            print(ui.hint("A notification is pushed when the AI waits for "
                          "a permission, a confirmation or an answer."))
        return True

    # A "/notify ..." with an argument that is not one of the four known
    # ones is still a notify command — a typo.  Consume it and show the
    # usage instead of letting it fall through to the model as a chat
    # message, which is what every unrecognised slash command does.
    if command_low.startswith("/notify ") or command_low.startswith("notify "):
        print(ui.notice("warn", "Usage: /notify [true|false|toggle|status]"))
        return True

    return False


# Notices emitted by the STT module while it is imported.  Collected rather
# than printed inline; see the comment inside the try block below.
STT_IMPORT_NOTICES: list[str] = []

try:
    # Importing the STT module prints its own notices (missing optional
    # dependencies, VAD fallbacks).  Those are dependency chatter rather than
    # interface output, so they are captured here and shown once — grouped
    # with the rest of the startup information — instead of scattering over
    # the banner.
    import contextlib
    import io

    _stt_stdout = io.StringIO()
    with contextlib.redirect_stdout(_stt_stdout):
        from main import listen

    STT_IMPORT_NOTICES = [
        line for line in _stt_stdout.getvalue().splitlines() if line.strip()
    ]

    check_stt = "where edge-tts" if sys.platform == "win32" else "which edge-tts"
    if subprocess.run(
        check_stt,
        shell=True,
        capture_output=True
    ).returncode != 0:
        raise Exception("edge-tts not found")

    check_mpv = "where mpv" if sys.platform == "win32" else "which mpv"
    if subprocess.run(
        check_mpv,
        shell=True,
        capture_output=True
    ).returncode != 0:
        raise Exception("mpv not found")

    HAS_STT = True

except Exception:
    HAS_STT = False

# Start sys diagnosis in background immediately (after HAS_STT so the
# diagnosis helpers are defined).
#
# The probe runs *quietly*.  It runs on a background thread while the chat
# prompt is already on screen, and the platform wrappers behind it print an
# "[EXECUTING] …" / "[OUT] …" trace.  That trace used to land in the middle
# of the user's prompt — and in the raw-mode fallback input path it even
# staircased across the screen, because a bare "\n" no longer returns the
# cursor to column 0.  The data itself still reaches the model as a system
# message (_get_diag_history), so nothing is lost by keeping the probe mute.
_diag_executor = ThreadPoolExecutor(max_workers=1)
_diag_future   = _diag_executor.submit(run_background_diagnosis)


def _get_diag_history():
    """Return a one-shot system message with diagnosis data, or None."""
    try:
        if _diag_future.done():
            result = _diag_future.result()
            if result:
                return {
                    "role": "system",
                    "content": (
                        "Here is background diagnostic data collected from the environment:\n"
                        f"{json.dumps(result, indent=2)}\n"
                        "Check if anything is genuinely concerning and inform the user. "
                        "If everything looks normal, say nothing about it."
                    )
                }
    except Exception:
        pass
    return None

# Shared display state is owned by display_state.py and toggled by
# input_handler.py so the shortcut is attached to the real PromptSession.
display_state.configure(CONFIG_PATH)


def _handle_session_command(command: str) -> None:
    """Handle /session subcommands: list, resume, save, delete, draft."""
    if list_sessions is None:
        print(ui.notice("error", "Session memory module not available"))
        return

    parts = command.strip().split()
    if len(parts) < 2:
        print(ui.hint("Usage: /session <list|resume|save|delete|draft> [session_id]"))
        print(ui.hint("  /session list           — List all saved sessions"))
        print(ui.hint("  /session resume <id>    — Resume a session (injects context)"))
        print(ui.hint("  /session save             — Finalize current session draft"))
        print(ui.hint("  /session delete <id>    — Delete a saved session"))
        print(ui.hint("  /session draft            — Show current draft"))
        return

    sub = parts[1].lower()

    try:
        if sub == "list":
            result = list_sessions()
            print(result)

        elif sub == "resume":
            if len(parts) < 3:
                print(ui.notice("error", "Usage: /session resume <session_id>"))
                return
            session_id = parts[2]
            print(ui.notice("info", f"Resuming session {session_id}..."))
            result = resume_session(session_id)
            print(ui.notice("ok", "Resumed"))
            # The resumed context would need to be injected into the current history
            # For now, just show the session content
            print(result)

        elif sub == "save":
            print(ui.notice("info", "Finalizing current session..."))
            path = finalize_session(use_small_model=False)  # Set True when API configured
            if path.startswith("[ERROR"):
                print(ui.notice("error", path))
            else:
                print(ui.notice("ok", f"Session saved to {path}"))

        elif sub == "delete":
            if len(parts) < 3:
                print(ui.notice("error", "Usage: /session delete <session_id>"))
                return
            session_id = parts[2]
            result = delete_session(session_id)
            if result.startswith("[ERROR]"):
                print(ui.notice("error", result))
            else:
                print(ui.notice("ok", result))

        elif sub == "draft":
            draft = get_session_draft()
            print(json.dumps(draft, ensure_ascii=False, indent=2))

        else:
            print(ui.notice("error", f"Unknown subcommand: {sub}"))
            print(ui.hint("Available: list, resume, save, delete, draft"))

    except Exception as e:
        print(ui.notice("error", f"Session error: {e}"))


def _plugin_state(plugin_name: str, info: dict | None = None) -> dict:
    """Connection state of a plugin.

    Uses ``plugin_manager.plugin_state`` when the manager provides it and
    falls back to the plain enabled/disabled information otherwise, so the
    command keeps working with a reduced manager.
    """
    info = info or {}
    getter = getattr(plugin_manager, "plugin_state", None)
    if callable(getter):
        try:
            state = getter(plugin_name)
            if isinstance(state, dict) and state:
                return state
        except Exception as exc:
            print(f"{GRAY}[Plugins] Setup state unavailable: {exc}{RESET}")
    return {
        "name": plugin_name,
        "enabled": bool(info.get("enabled")),
        "description": info.get("description", ""),
        "requires_setup": False,
        "authorized": True,
        "account": "",
        "client_configured": True,
        "setup_hint": "",
    }


def _run_plugin_setup(plugin_name: str) -> bool:
    """Run the Google consent flow for a plugin.

    Prints the URL the user has to open, waits for the approval and stores the
    token.  Returns True when the account is connected afterwards.
    """
    service_getter = getattr(plugin_manager, "get_plugin_service", None)
    service = service_getter(plugin_name) if callable(service_getter) else None
    if not service:
        return False

    try:
        from core import google_oauth
    except Exception as exc:
        print(f"{RED}[Plugins] The Google setup module is unavailable: {exc}{RESET}")
        return False

    result = google_oauth.run_setup(service)

    if result.get("stage") == "need_client":
        print(google_oauth.client_setup_instructions(service))
        try:
            client_id = input("\n  Client ID: ").strip()
            client_secret = input("  Client secret (Enter to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{YELLOW}Setup cancelled.{RESET}")
            return False
        if not client_id:
            print(f"{YELLOW}No client id given — setup cancelled.{RESET}")
            return False
        print(f"{GRAY}  Saved the client credentials to config/google_oauth.json{RESET}")
        result = google_oauth.run_setup(service, client_id=client_id,
                                        client_secret=client_secret)

    if result.get("stage") == "blocked":
        # Google refused the client/redirect combination.  Say why and show
        # the fix — a bare "setup failed" is what made this so confusing.
        print(f"\n{RED}[✗] '{plugin_name}' setup was blocked by Google.{RESET}")
        print(f"{GRAY}  {result.get('message', '')}{RESET}")
        print(google_oauth.client_setup_instructions(service))
        return False

    if result.get("stage") == "no_code":
        # The approval never came back — most likely the link was opened on
        # another device, where the local redirect cannot reach us.  The
        # redirect URL in that browser's address bar is still a valid code.
        pasted = ""
        if sys.stdin.isatty():
            try:
                pasted = input(
                    f"\n  {CYAN}Paste the redirect URL or the code "
                    f"(Enter to skip):{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                pasted = ""
        if pasted:
            ok, detail = google_oauth.complete_code_flow(service, pasted)
            if ok:
                print(f"\n{GREEN}[✓] '{plugin_name}' is connected.{RESET}")
                return True
            print(f"\n{RED}[✗] Google rejected that code: {detail}{RESET}")
            print(f"{GRAY}Try again with '/plugin setup {plugin_name}'.{RESET}")
            return False
        print(f"\n{YELLOW}[!] The approval never arrived.{RESET}")
        print(f"{GRAY}  Open the link on this device, or paste the redirect URL "
              f"after approving on another device.{RESET}")
        print(f"{GRAY}  Then run '/plugin setup {plugin_name}' again.{RESET}")
        return False

    if result.get("ok"):
        account = result.get("account") or ""
        print(f"\n{GREEN}[✓] '{plugin_name}' is connected"
              + (f" as {account}" if account else "") + f".{RESET}")
        return True

    message = result.get("message") or "the setup did not finish"
    print(f"\n{RED}[✗] '{plugin_name}' setup failed: {message}{RESET}")
    print(f"{GRAY}You can try again with '/plugin setup {plugin_name}'.{RESET}")
    return False


def _setup_whatsapp() -> None:
    """The WhatsApp pairing path behind '/plugin setup whatsapp'.

    The pairing code is rendered once, here, because the user asked for
    it.  The gateway's event loop never prints it: an unpaired gateway
    stays silent for the rest of the session instead of reprinting a
    rotating secret into the middle of the chat.

    When the session is already linked there is no code to give — a QR is
    only wanted for a re-link — so the gateway is killed and the plugin is
    switched off instead.
    """
    global config

    setup = getattr(plugin_manager, "setup", None)
    if not callable(setup):
        print(f"{YELLOW}[WhatsApp] The plugin manager cannot run the "
              f"pairing step.{RESET}")
        return

    try:
        result = setup("whatsapp") or {}
    except Exception as exc:
        print(f"{RED}[WhatsApp] The pairing step failed: {exc}{RESET}")
        return

    state = result.get("state", "")
    message = result.get("message") or ""
    if result.get("ok"):
        color = YELLOW if state == "reset" else GREEN
        print(f"{color}[✓] {message}{RESET}")
    else:
        print(f"{RED}[✗] {message}{RESET}")

    # The setup may have switched the plugin on or off — pick that up.
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        pass


# Every subcommand the ``/plugin`` command understands.
PLUGIN_SUBCOMMANDS = {
    "list", "status", "enable", "disable", "setup", "auth", "connect",
    "disconnect", "revoke", "logout",
}


def _parse_plugin_args(parts: list[str]) -> tuple[str, str]:
    """Split ``/plugin ...`` into ``(subcommand, plugin name)``.

    Both orders are accepted: ``/plugin setup gmail`` (the documented form)
    and ``/plugin gmail setup`` (the form people naturally type).  The old
    hints advertised the second one while the parser only understood the
    first — a small lie that cost the user an afternoon.
    """
    if len(parts) < 2:
        return "", ""
    first = parts[1].lower()
    second = parts[2].lower() if len(parts) > 2 else ""

    if first in PLUGIN_SUBCOMMANDS:
        return first, second
    if second in PLUGIN_SUBCOMMANDS:
        return second, first
    return first, second


def _handle_plugin_command(command: str) -> None:
    """Handle /plugin subcommands: list, enable, disable, status, setup."""
    global config
    parts = command.strip().split()
    if len(parts) < 2:
        # No subcommand - show all plugins and their status (same as /plugin list)
        _show_all_plugins()
        return

    sub, plugin_name = _parse_plugin_args(parts)

    if sub == "list":
        _show_all_plugins()
        return

    if sub == "status":
        if not plugin_name:
            # /plugin status without name - show all plugins
            _show_all_plugins()
            return
        info = plugin_manager.get_plugin_info(plugin_name)
        if info:
            status = f"{GREEN}ENABLED{RESET}" if info["enabled"] else f"{RED}DISABLED{RESET}"
            print(f"\n  {CYAN}{plugin_name}{RESET}")
            print(f"  Status:  {status}")
            print(f"  Description: {info['description']}")
            # The runtime is what makes an enabled plugin usable — a plugin
            # that is on while its service is down still fails every call.
            runtime_getter = getattr(plugin_manager, "runtime_state", None)
            if callable(runtime_getter):
                runtime = runtime_getter(plugin_name) or {}
                if runtime.get("state") not in ("no_runtime", "unknown"):
                    mark = f"{GREEN}RUNNING{RESET}" if runtime.get("ok") else f"{YELLOW}NOT RUNNING{RESET}"
                    print(f"  Runtime: {mark}  {GRAY}{runtime.get('message', '')}{RESET}")
                    # The pairing state is only asked for here, in an
                    # explicit status command — it is never volunteered in
                    # the middle of a chat.
                    pairing = (runtime.get("detail") or {}).get("pairing_state")
                    if pairing == "READY":
                        print(f"  Pairing: {GREEN}LINKED{RESET}")
                    elif pairing == "QR_REQUIRED":
                        print(f"  Pairing: {YELLOW}NOT PAIRED{RESET}  "
                              f"{GRAY}('/plugin setup whatsapp' shows the code){RESET}")
                    elif pairing:
                        print(f"  Pairing: {GRAY}{pairing}{RESET}")
            state = _plugin_state(plugin_name, info)
            if state.get("requires_setup"):
                if state.get("authorized"):
                    account = state.get("account") or ""
                    print(f"  Account: {GREEN}CONNECTED{RESET}"
                          + (f"  {GRAY}({account}){RESET}" if account else ""))
                elif state.get("client_configured"):
                    print(f"  Account: {YELLOW}NOT CONNECTED{RESET}")
                else:
                    print(f"  Account: {YELLOW}NO OAUTH CLIENT CONFIGURED{RESET}")
                print(f"  Setup:   {CYAN}/plugin setup {plugin_name}{RESET}")
            if info["enabled"]:
                tools = plugin_manager.get_enabled_plugin_tools(plugin_name)
                if tools:
                    print(f"  Available tools ({len(tools)}):")
                    for t in tools:
                        print(f"    - {t}")
        else:
            print(f"{RED}Unknown plugin: '{plugin_name}'{RESET}")
        return

    if sub in ("setup", "auth", "connect"):
        if not plugin_name:
            print(f"{RED}Plugin name required. Usage: /plugin {sub} <plugin_name>{RESET}")
            return
        info = plugin_manager.get_plugin_info(plugin_name)
        if not info:
            print(f"{RED}Unknown plugin: '{plugin_name}'{RESET}")
            print(f"{GRAY}Available: {', '.join(plugin_manager.list_plugins().keys())}{RESET}")
            return
        state = _plugin_state(plugin_name, info)
        if not state.get("requires_setup"):
            if plugin_name == "whatsapp":
                # The pairing path.  The code is shown once, here, because
                # the user asked for it — the gateway itself stays quiet.
                _setup_whatsapp()
                return
            print(f"{GRAY}'{plugin_name}' runs locally — no account setup needed.{RESET}")
            # Activation has to start the runtime, not just flip the flag:
            # an enabled WhatsApp plugin whose gateway is down still fails
            # every call.  It is safe to run even when the plugin is already
            # on — activating a running gateway is a no-op.
            activate = getattr(plugin_manager, "activate", None)
            if callable(activate):
                result = activate(plugin_name)
                if not result.get("ok"):
                    print(f"{RED}[✗] Unknown plugin: '{plugin_name}'{RESET}")
                    if result.get("message"):
                        print(f"{RED}{result.get('message')}{RESET}")
                    print(f"{GRAY}Available: {', '.join(plugin_manager.list_plugins().keys())}{RESET}")
                    return
                print(f"{GREEN}[✓] Plugin '{plugin_name}' enabled.{RESET}")
                runtime = result.get("runtime") or {}
                runtime_message = runtime.get("message")
                if runtime.get("ok"):
                    if runtime_message:
                        print(f"{GRAY}  {runtime_message}{RESET}")
                elif runtime_message and runtime.get("state") != "no_runtime":
                    print(f"{YELLOW}[!] {runtime_message}{RESET}")
            else:
                if not plugin_manager.is_enabled(plugin_name):
                    plugin_manager.enable(plugin_name)
                print(f"{GREEN}[✓] Plugin '{plugin_name}' enabled.{RESET}")
            return
        if not plugin_manager.is_enabled(plugin_name):
            plugin_manager.enable(plugin_name)
            print(f"{GREEN}[✓] Plugin '{plugin_name}' enabled.{RESET}")
        _run_plugin_setup(plugin_name)
        return

    if sub in ("disconnect", "revoke", "logout"):
        if not plugin_name:
            print(f"{RED}Plugin name required. Usage: /plugin {sub} <plugin_name>{RESET}")
            return
        info = plugin_manager.get_plugin_info(plugin_name)
        if not info:
            print(f"{RED}Unknown plugin: '{plugin_name}'{RESET}")
            return
        service_getter = getattr(plugin_manager, "get_plugin_service", None)
        service = service_getter(plugin_name) if callable(service_getter) else None
        if not service:
            print(f"{GRAY}'{plugin_name}' has no account to disconnect.{RESET}")
            return
        try:
            from core import google_oauth
            google_oauth.revoke_token(service)
            print(f"{GREEN}[✓] '{plugin_name}' disconnected — the access was revoked.{RESET}")
            print(f"{GRAY}Run '/plugin setup {plugin_name}' to connect it again.{RESET}")
        except Exception as exc:
            print(f"{RED}[✗] Could not disconnect '{plugin_name}': {exc}{RESET}")
        return

    if sub in ("enable", "disable"):
        if not plugin_name:
            print(f"{RED}Plugin name required. Usage: /plugin {sub} <plugin_name>{RESET}")
            return

        # A manager that knows about runtimes (the real one) enables *and*
        # starts the plugin; a reduced manager falls back to the plain flag.
        activate = getattr(plugin_manager, "activate", None)
        deactivate = getattr(plugin_manager, "deactivate", None)

        if sub == "enable" and callable(activate):
            result = activate(plugin_name)
            if not result.get("ok"):
                print(f"{RED}[✗] Unknown plugin: '{plugin_name}'{RESET}")
                if result.get("message"):
                    print(f"{RED}{result.get('message')}{RESET}")
                print(f"{GRAY}Available: {', '.join(plugin_manager.list_plugins().keys())}{RESET}")
                return
            print(f"{GREEN}[✓] Plugin '{plugin_name}' enabled.{RESET}")
            runtime = result.get("runtime") or {}
            runtime_ok = runtime.get("ok")
            runtime_state = runtime.get("state")
            runtime_message = runtime.get("message")
            if runtime_ok:
                if runtime_message:
                    print(f"{GRAY}  {runtime_message}{RESET}")
            elif runtime_message and runtime_state != "no_runtime":
                print(f"{YELLOW}[!] {runtime_message}{RESET}")
            # Reload config to pick up the change
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    config = json.load(f)
            except Exception:
                pass
            # An account plugin is useless until the user granted access,
            # so enabling it starts the consent flow right away.
            info = plugin_manager.get_plugin_info(plugin_name) or {}
            state = _plugin_state(plugin_name, info)
            if state.get("requires_setup") and not state.get("authorized"):
                print(f"{YELLOW}[!] '{plugin_name}' needs access to your "
                      f"Google account — starting the permission request.{RESET}")
                _run_plugin_setup(plugin_name)
                return
            tools = result.get("tools") or []
            if tools:
                if runtime_ok or runtime_state == "no_runtime":
                    print(f"{GREEN}[✓] Its {len(tools)} tools are now in my tool "
                          f"context — say 'try again' and I can use them right "
                          f"away.{RESET}")
                else:
                    print(f"{YELLOW}[!] Its {len(tools)} tools are advertised but will fail until the runtime is up. Use '/plugin status {plugin_name}' to see the state.{RESET}")
            if plugin_name == "whatsapp":
                # No pairing code here: it is only rendered when the user
                # asks for it with '/plugin setup whatsapp'.
                print(f"{GRAY}  (Run '/plugin setup whatsapp' if this device "
                      f"is not paired yet.){RESET}")
            return

        if sub == "disable" and callable(deactivate):
            result = deactivate(plugin_name)
            if not result.get("ok"):
                print(f"{RED}[✗] Unknown plugin: '{plugin_name}'{RESET}")
                print(f"{GRAY}Available: {', '.join(plugin_manager.list_plugins().keys())}{RESET}")
                return
            print(f"{YELLOW}[✓] Plugin '{plugin_name}' disabled.{RESET}")
            runtime = result.get("runtime") or {}
            if runtime.get("message"):
                print(f"{GRAY}  {runtime.get('message')}{RESET}")
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    config = json.load(f)
            except Exception:
                pass
            return

        if sub == "enable":
            ok = plugin_manager.enable(plugin_name)
            verb, color = "enabled", GREEN
        else:
            ok = plugin_manager.disable(plugin_name)
            verb, color = "disabled", YELLOW
        if ok:
            print(f"{color}[✓] Plugin '{plugin_name}' {verb}.{RESET}")
            # Reload config to pick up the change
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    config = json.load(f)
            except Exception:
                pass
            if sub == "enable":
                # An account plugin is useless until the user granted access,
                # so enabling it starts the consent flow right away.
                info = plugin_manager.get_plugin_info(plugin_name) or {}
                state = _plugin_state(plugin_name, info)
                if state.get("requires_setup") and not state.get("authorized"):
                    print(f"{YELLOW}[!] '{plugin_name}' needs access to your "
                          f"Google account — starting the permission request.{RESET}")
                    _run_plugin_setup(plugin_name)
        else:
            print(f"{RED}[✗] Unknown plugin: '{plugin_name}'{RESET}")
            print(f"{GRAY}Available: {', '.join(plugin_manager.list_plugins().keys())}{RESET}")
        return

    print(f"{RED}Unknown subcommand: {sub}{RESET}")
    print(f"{GRAY}Available: list, enable, disable, status, setup, disconnect{RESET}")


def _show_all_plugins() -> None:
    """Display all plugins with their status."""
    plugins = plugin_manager.list_plugins()
    print(ui.banner("Plugins", f"{len(plugins)} registered"))
    rows = []
    for name, info in plugins.items():
        status = "● ENABLED" if info["enabled"] else "○ DISABLED"
        desc = info["description"]
        extra = ""
        state = _plugin_state(name, info)
        if state.get("requires_setup"):
            if state.get("authorized"):
                account = state.get("account") or ""
                extra = "  ● CONNECTED" + (f"  ({account})" if account else "")
            elif state.get("client_configured"):
                extra = "  ○ NEEDS SETUP"
            else:
                extra = "  ○ NO OAUTH CLIENT"
        rows.append((f"  {name}", f"{status}  {desc}{extra}"))
    if rows:
        for line in ui.key_value_rows(rows, key_width=22, tone="muted"):
            print(line)
    print()

def _show_help() -> None:
    """Display a short help listing of available slash commands."""
    cmds = [
        "/autonomous [on|off|status|toggle]",
        "/agent [auto|...]",
        "/plugin list | /plugin setup <name> | /plugin status <name>",
        "/plugin enable <name> | /plugin disable <name>",
        "/plugin disconnect <name>",
        "/session [list|resume|save|delete|draft]",
        "/expand /e   (show every tool argument, the full write payload,",
        "              the delegation command and the sub-AI's tool output)",
        "/collapse /c",
        "/notify [true|false|toggle|status]   (ping me when the AI waits",
        "                             for a permission, a confirmation or an",
        "                             answer)",
        "/normal /chat",
        "/help /commands",
    ]
    print(ui.heading("Available commands", "info"))
    for c in cmds:
        print(f"  {CYAN}{c}{RESET}")
    print()

def _pending_plugin_setups() -> list[dict]:
    """Account plugins that are switched on but have not been granted access."""
    pending = []
    for name in plugin_manager.list_plugins():
        state = _plugin_state(name)
        if (state.get("requires_setup") and state.get("enabled")
                and not state.get("authorized")):
            pending.append(state)
    return pending


def _notify_plugin_setups() -> None:
    """Warn about enabled-but-unconnected plugins when the session starts.

    Enabling a Google plugin is only half the job — until the account has
    granted access its tools stay hidden from the model.  Without this notice
    a plugin can be switched on in config.json and then silently do nothing,
    which is the confusing state this whole flow exists to prevent.
    """
    pending = _pending_plugin_setups()
    if not pending:
        return

    names = ", ".join(f"'{s.get('name', '')}'" for s in pending)
    noun = "plugin" if len(pending) == 1 else "plugins"
    print(ui.notice("warn", f"{noun.capitalize()} {names} enabled but not connected — the account has not been granted access yet, so the tools stay hidden."))
    for state in pending:
        print(ui.hint(f"/plugin setup {state.get('name', '')} — shows the URL to approve"))

    # Only offer the interactive jump when someone is actually there.  In a
    # piped or scripted session an input() would swallow the next real line.
    if not sys.stdin.isatty():
        return

    name = pending[0].get("name", "")
    try:
        answer = input(ui.prompt(f"Connect '{name}' now? [y/N]")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if answer in ("y", "yes"):
        _run_plugin_setup(name)


def _other_sessions() -> list:
    """The pids of other running Termux-AI sessions.

    Two sessions share one WhatsApp gateway, and each of them opens its own
    WebSocket listener to it — so every incoming message would be handled
    twice and the busy auto-reply would answer twice.  The sessions cannot
    see each other in memory, so they have to be found in the process table.
    """
    pids = []
    me = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmdline = fh.read().decode("utf-8", "replace").replace("\0", " ")
        except OSError:
            continue
        if "core/interface.py" in cmdline and "python" in cmdline:
            pids.append(pid)
    return pids


def _start_whatsapp_runtime() -> bool:
    """Start the WhatsApp runtime when the plugin is enabled.

    An enabled plugin has to be *running*: the node gateway (bot.js) is
    started here as well, otherwise every WhatsApp tool would fail with
    "connection refused" until the plugin was enabled again.
    """
    if not plugin_manager.is_enabled("whatsapp"):
        print(ui.notice("muted", "WhatsApp plugin disabled — manager not started. Use '/plugin enable whatsapp' to start."))
        return False
    if whatsapp_manager is None:
        print(ui.notice("warn", "Termux-WP is not installed; the gateway cannot start. Install Termux-WP to use WhatsApp tools."))
        return False
    try:
        ok, message = whatsapp_manager.ensure_running()
        if not ok:
            print(ui.notice("warn", f"The plugin is enabled but its runtime did not start: {message}"))
            print(ui.notice("muted", "The WhatsApp tools will fail until it is up — '/plugin status whatsapp' shows the state."))
        return bool(ok)
    except Exception as e:
        print(ui.notice("error", f"Failed to start WhatsApp Manager: {e}"))
        return False


def chat_loop():
    # Start WhatsApp Manager (only if plugin is enabled)
    log_write("—" * 20)
    # Session marker in the live log: the first line of a run, so a log
    # that is read after the fact shows where one session ended and the next
    # began.
    runtime_log.log_note(
        f"session started (pid {os.getpid()}) — live log: "
        f"{runtime_log.log_path()}",
        "session",
    )
    global config

    # A second session would open a second listener on the same gateway and
    # answer every incoming message twice.
    others = _other_sessions()
    if others:
        print(ui.notice("warn", "Another Termux-AI session is running (pid " +
              ", ".join(str(p) for p in others) +
              "). Both would open a WhatsApp listener on the same gateway, "
              "so every incoming message could be answered twice — "
              "close the other one if you want a single reply."))

    _start_whatsapp_runtime()

    # A plugin can be switched on in config.json without the account ever
    # having granted access; say so now instead of failing silently later.
    _notify_plugin_setups()

    history: list[dict] = []
    agent_history: list[dict] = []
    agent_mode = False
    _diag_injected = False

    # ── Startup UI ──────────────────────────────────────────────────────
    # The banner, import notices, and a single status row replace the old
    # scattered prints so the terminal always shows a clean, aligned header.

    banner_lines = ui.banner("Termux-AI", "Terminal AI ready.")
    if banner_lines:
        print(banner_lines)
        # A rule separates the header from the status row below it, so the
        # settings are not read as part of the banner.
        print(ui.rule())

    # STT import notices (missing optional deps, VAD fallbacks)
    for line in STT_IMPORT_NOTICES:
        print(ui.notice("muted", line))

    # Single status row with the essential settings.
    detail_state = "EXPANDED" if display_state.is_expanded() else "COLLAPSED"
    ai_mode = "AUTONOMOUS" if config.get("autonomous", False) else "NON-AUTONOMOUS"
    tts_mode = "voice" if (HAS_STT and config.get("tts_enabled")) else "text"
    notify_on = bool(config.get("notify", True))
    print(ui.status_row([
        ("mode", ai_mode, "info"),
        ("details", detail_state, "muted"),
        ("input", tts_mode, "muted"),
        ("notify", "ON" if notify_on else "OFF",
         "ok" if notify_on else "muted"),
    ]))

    # Keyboard hint line.
    print(ui.hint("Enter = send  •  Ctrl+N = new line  •  Ctrl+O (or /expand, /collapse) = toggle details"))

    if HAS_STT and config.get("tts_enabled"):
        try:
            greeting_prompt = (
                "SYSTEM: Start the conversation naturally like a friendly assistant. "
                "Avoid robotic introductions, capability lists, or mentioning tools unless asked. "
                "Keep the tone warm and casual."
            )

            # Inject diagnosis into greeting if already done
            diag_msg = _get_diag_history()
            greeting_history = [diag_msg] if diag_msg else []
            if diag_msg:
                _diag_injected = True

            print("\nAI (Voice) > ")
            runtime_log.log_user_input(greeting_prompt, source="greeting")
            reply = ask_ai(greeting_prompt, history=greeting_history, voice=config.get("tts_enabled", False))
            print(render_markdown_terminal(reply))
            speak(reply, block=True)
            runtime_log.log_assistant_reply(reply, voice=True)
            history.append({"role": "user",      "content": greeting_prompt})
            history.append({"role": "assistant",  "content": reply})
        except:
            pass

    while True:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded_config = json.load(f)
            if isinstance(loaded_config, dict):
                config = loaded_config
        except Exception as e:
            # A missing or corrupt config.json must not kill the session —
            # keep the last known-good settings and carry on.
            print(ui.notice("warn", f"Could not read config.json ({e}); keeping the previous settings."))
        if not config.get("tts_enabled") or not HAS_STT:
            try:
                user_input = get_interactive_input(ui.prompt("YOU"))
            except EOFError:
                break

        else:
            print(ui.notice("info", "Listening..."))
            try:
                user_input = listen(once=True, calibrate_once=True, use_groq=config.get("use_groq", False))
                if user_input:
                    print(f"\n{ui.prompt('YOU (Voice)')}{user_input}")
                else:
                    print(ui.notice("muted", "No speech detected"))
                    continue
            except KeyboardInterrupt:
                print(ui.notice("info", "Voice mode cancelled. Switching to typing mode."))
                config = _update_config(tts_enabled=False)
                continue
            except Exception as e:
                print(ui.notice("error", f"STT error: {e}"))
                continue

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "exit.", "quit."):
            print(ui.notice("info", "Session ended."))
            break
        if user_input.lower() in ["start voice.", "start voice"]:
            config = _update_config(tts_enabled=True)
            continue
        if user_input.lower() in ["start voice local.", "start voice local"]:
            config = _update_config(tts_enabled=True, use_groq=False)
            continue
        if user_input.lower() in ["start voice remote.", "start voice remote"]:
            config = _update_config(tts_enabled=True, use_groq=True)
            continue
        if user_input.lower() in ("stop voice.", "stop voice"):
            config = _update_config(tts_enabled=False)
            continue

        command = user_input.strip()
        command_low = command.casefold()

        # The user's input is logged the moment it is received — before the
        # model is even asked.  A session that is cut short still shows what
        # was asked, and the log starts before the first tool call does.
        if command_low.startswith("/"):
            runtime_log.log_event("command", command, header="slash command")
        else:
            runtime_log.log_user_input(
                user_input, source="agent" if agent_mode else "chat"
            )

        # Autonomous mode commands
        if command_low in ("/autonomous on", "autonomous on", "/autonomous enable", "autonomous enable", "/autonomous 1", "/autonomous true"):
            config = _update_config(autonomous=True)
            print(ui.notice("info", "AI Mode: AUTONOMOUS (Permission layer bypassed)"))
            continue

        if command_low in ("/autonomous off", "autonomous off", "/autonomous disable", "autonomous disable", "/autonomous 0", "/autonomous false"):
            config = _update_config(autonomous=False)
            print(ui.notice("info", "AI Mode: NON-AUTONOMOUS (Permission layer active)"))
            continue

        if command_low in ("/autonomous status", "autonomous status"):
            status_str = "AUTONOMOUS (Permission layer bypassed)" if config.get("autonomous", False) else "NON-AUTONOMOUS (Permission layer active)"
            print(ui.notice("info", f"AI Mode is currently: {status_str}"))
            continue

        if command_low in ("/autonomous", "autonomous", "/autonomous toggle", "autonomous toggle"):
            new_state = not config.get("autonomous", False)
            config = _update_config(autonomous=new_state)
            status_str = "AUTONOMOUS (Permission layer bypassed)" if new_state else "NON-AUTONOMOUS (Permission layer active)"
            print(ui.notice("info", f"AI Mode toggled to: {status_str}"))
            continue

        # Legacy project runner: /agent auto
        if command_low in ("/agent auto", "agent auto"):
            import llm_client
            while True:
                result = llm_client.run_agent_step(voice=config.get("tts_enabled", False))
                print(render_markdown_terminal(f"**Agent Status:** {result}"))
                if "No pending" in result or "failed" in result.lower():
                    break
            # Send notification when agent auto work is done
            if config.get("notify", True):
                _send_notification("Termux-AI Agent", "Agent auto task completed")
            continue

        # Direct persistent agent mode.
        if command_low == "/agent" or command_low.startswith("/agent "):
            agent_mode = True
            inline_prompt = command[6:].strip()
            print(ui.notice("info", "Agent mode enabled. Use /normal to return."))
            if inline_prompt:
                try:
                    print(ui.notice("info", "Agent Thinking"))
                    runtime_log.log_user_input(inline_prompt, source="agent")
                    reply = ask_agent(
                        inline_prompt,
                        history=list(agent_history),
                        voice=config.get("tts_enabled", False),
                    )
                    print("\nAgent AI >")
                    print(render_markdown_terminal(reply))
                    runtime_log.log_assistant_reply(reply, agent=True)
                    if config.get("tts_enabled") and HAS_STT:
                        speak(reply, block=True)
                    metadata = get_last_response_metadata()
                    assistant_msg = {"role": "assistant", "content": reply}
                    if metadata.get("reasoning_details"):
                        assistant_msg["reasoning_details"] = metadata["reasoning_details"]
                        assistant_msg["_reasoning_provider"] = metadata.get("reasoning_provider")
                    agent_history.extend([
                        {"role": "user", "content": inline_prompt},
                        assistant_msg,
                    ])
                    # Send notification when agent inline task is done
                    if config.get("notify", True):
                        _send_notification("Termux-AI Agent", "Agent task completed")
                except KeyboardInterrupt:
                    print(ui.notice("warn", "Interrupted."))
                except Exception as e:
                    print(ui.notice("error", f"Error: {e}"))
            continue

        if command_low in ("/normal", "/chat"):
            agent_mode = False
            print(ui.notice("info", "Agent mode disabled. Returning to normal AI."))
            continue

        # Text-command alternative to the Ctrl+O keybinding: some terminals
        # never pass Ctrl+O through to the app at all, so this is a reliable
        # fallback. Checked here (before the agent_mode split below) so it
        # works identically in both /agent and normal chat.
        if command_low in ("/expand", "/e", "/e on", "/details on", "/view extended", "/view expanded"):
            display_state.set_expanded(True)
            print(ui.notice("ok", "Tool/Reasoning details: EXPANDED"))
            continue

        if command_low in ("/collapse", "/c", "/c off", "/details off", "/view collapsed", "/view compact"):
            display_state.set_expanded(False)
            print(ui.notice("ok", "Tool/Reasoning details: COLLAPSED"))
            continue
        # Notifications.  The command used to switch the flag silently —
        # no output at all, so "/notify true" looked like it had been
        # ignored.  It now confirms, reports the current state, and proves
        # the plumbing works by sending one test notification.
        if _handle_notify_command(command_low):
            continue
        if command_low in ("/help", "/commands"):
            _show_help()
            continue
        # Session memory commands
        if command_low.startswith("/session"):
            _handle_session_command(command)
            continue

        # Plugin commands
        if command_low.startswith("/plugin"):
            _handle_plugin_command(command)
            continue

        if agent_mode:
            try:
                print(ui.notice("info", "Agent Thinking"))
                reply = ask_agent(
                    user_input,
                    history=list(agent_history),
                    voice=config.get("tts_enabled", False),
                )
            except KeyboardInterrupt:
                print(ui.notice("warn", "Interrupted."))
                continue
            except Exception as e:
                print(ui.notice("error", f"Error: {e}"))
                continue

            print("\nAgent AI >")
            print(render_markdown_terminal(reply))
            runtime_log.log_assistant_reply(reply, agent=True)
            if config.get("tts_enabled") and HAS_STT:
                speak(reply, block=True)
            metadata = get_last_response_metadata()
            assistant_msg = {"role": "assistant", "content": reply}
            if metadata.get("reasoning_details"):
                assistant_msg["reasoning_details"] = metadata["reasoning_details"]
                assistant_msg["_reasoning_provider"] = metadata.get("reasoning_provider")
            agent_history.extend([
                {"role": "user", "content": user_input},
                assistant_msg,
            ])
            # Send notification when agent mode task is done
            if config.get("notify", True):
                _send_notification("Termux-AI Agent", "Agent task completed")
            continue

        print(ui.notice("info", "Thinking"))

        # Inject diagnosis on first user message if not already done
        call_history = list(history)
        if not _diag_injected:
            diag_msg = _get_diag_history()
            if diag_msg:
                call_history = [diag_msg] + call_history
                _diag_injected = True

        # Open a new chunk for this turn
        _cm.open_chunk(user_input)

        # Prepend chunk-based history (summaries + recent raw) before any
        # other history items. Chunk history goes first so the model sees
        # the full conversation arc before the current session's messages.
        chunk_history = _cm.build_history()
        if chunk_history:
            call_history = chunk_history + call_history

        try:
            reply = ask_ai(
                user_input,
                history=call_history,
                voice=config.get("tts_enabled", False),
            )

        except KeyboardInterrupt:
            print(ui.notice("warn", "Interrupted."))
            continue

        except Exception as e:
            print(ui.notice("error", f"Error: {e}"))
            continue

        if config.get("tts_enabled") and HAS_STT:
            print("\nAI (Voice) >")
            runtime_log.log_assistant_reply(reply, voice=True)
        else:
            print("\nAI >")
            runtime_log.log_assistant_reply(reply)

        print(render_markdown_terminal(reply))
        if config.get("tts_enabled") and HAS_STT:
            speak(reply, block=True)

        # Close the chunk with the final reply, then trigger background summarization.
        _cm.close_chunk(reply)
        _cm.maybe_summarize_async()


if __name__ == "__main__":
    chat_loop()
