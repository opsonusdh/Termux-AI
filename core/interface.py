import os
import json
import sys
import time
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
from core.renderer import render_markdown_terminal, GRAY, RESET, RED
from core.input_handler import get_interactive_input
import core.display_state as display_state
from tools import *
import context_manager as _cm

# Config
BASE_DIR    = _ROOT
CONFIG_PATH = paths.CONFIG_FILE
DEFAULT_CONFIG = {
    "stt_path":    os.path.join(BASE_DIR, "Termux-STT"),
    "tts_enabled": False,
    "use_groq":    False,
    "show_details": False,
    "autonomous": False,
    "notify": True,
}
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

STT_PATH = os.path.expanduser(config["stt_path"])
if STT_PATH not in sys.path:
    sys.path.append(STT_PATH)


def _send_notification(title: str, message: str) -> None:
    """Send a system notification using termux-notification."""
    try:
        subprocess.run(
            ["termux-notification", "--title", title, "--content", message],
            capture_output=True,
            timeout=5
        )
    except Exception:
        pass  # Silently fail if notification fails


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

try:
    from main import listen

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

# Start sys diagnosis in background immediately (after HAS_STT so run_diagnosis is defined)
_diag_executor = ThreadPoolExecutor(max_workers=1)
_diag_future   = _diag_executor.submit(run_diagnosis)


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


def chat_loop():
    # Start WhatsApp Manager
    log_write("—"*20)
    global config
    try:
        whatsapp_manager.start()
    except Exception as e:
        print(f"{RED}[Whatsapp] Failed to start WhatsApp Manager: {e}{RESET}")

    history: list[dict] = []
    agent_history: list[dict] = []
    agent_mode = False
    _diag_injected = False

    # Display initial status
    print(f"{GRAY}[Info] Enter = send | Ctrl+N = new line | Ctrl+O (or /expand, /collapse) = toggle details. Current: {'EXPANDED' if display_state.is_expanded() else 'COLLAPSED'} | AI Mode: {'AUTONOMOUS' if config.get('autonomous', False) else 'NON-AUTONOMOUS'}{RESET}")

    print("Terminal AI ready. Type 'exit' to quit.")
    if HAS_STT:
        if not config.get("tts_enabled"):
            print("""Enter "start voice" to use Voice Input.""")
        else:
            print("""Say "stop voice" to use keyboard Input.""")
    
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
            reply = ask_ai(greeting_prompt, history=greeting_history, voice=config.get("tts_enabled", False))
            print(render_markdown_terminal(reply))
            speak(reply, block=True)
            log_write(f"\nAI (Voice) > {reply}")
            history.append({"role": "user",      "content": greeting_prompt})
            history.append({"role": "assistant",  "content": reply})
        except:
            pass
    else:
        print()

    while True:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
        if not config.get("tts_enabled") or not HAS_STT:
            try:
                user_input = get_interactive_input("\nYOU > ")
            except EOFError:
                break

        else:
            print(f"{GRAY}[Listening...]{RESET}")
            try:
                user_input = listen(once=True, calibrate_once=True, use_groq=config.get("use_groq", False))
                if user_input:
                    print(f"\nYOU (Voice) > {user_input}")
                else:
                    print(f"{GRAY}[No speech detected]{RESET}")
                    continue
            except KeyboardInterrupt:
                print(f"\n{GRAY}[Voice mode cancelled. Switching to typing mode]{RESET}")
                config = _update_config(tts_enabled=False)
                continue
            except Exception as e:
                print(f"\n[STT ERROR] {e}")
                continue

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "exit.", "quit."):
            print("Session ended.")
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

        # Autonomous mode commands
        if command_low in ("/autonomous on", "autonomous on", "/autonomous enable", "autonomous enable", "/autonomous 1", "/autonomous true"):
            config = _update_config(autonomous=True)
            print(f"{GRAY}[AI Mode: AUTONOMOUS (Permission layer bypassed)]{RESET}")
            continue

        if command_low in ("/autonomous off", "autonomous off", "/autonomous disable", "autonomous disable", "/autonomous 0", "/autonomous false"):
            config = _update_config(autonomous=False)
            print(f"{GRAY}[AI Mode: NON-AUTONOMOUS (Permission layer active)]{RESET}")
            continue

        if command_low in ("/autonomous status", "autonomous status"):
            status_str = "AUTONOMOUS (Permission layer bypassed)" if config.get("autonomous", False) else "NON-AUTONOMOUS (Permission layer active)"
            print(f"{GRAY}[AI Mode is currently: {status_str}]{RESET}")
            continue

        if command_low in ("/autonomous", "autonomous", "/autonomous toggle", "autonomous toggle"):
            new_state = not config.get("autonomous", False)
            config = _update_config(autonomous=new_state)
            status_str = "AUTONOMOUS (Permission layer bypassed)" if new_state else "NON-AUTONOMOUS (Permission layer active)"
            print(f"{GRAY}[AI Mode toggled to: {status_str}]{RESET}")
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
            print(f"{GRAY}[Agent mode enabled. Use /normal to return.]{RESET}")
            if inline_prompt:
                try:
                    print("\n[Agent Thinking]")
                    reply = ask_agent(
                        inline_prompt,
                        history=list(agent_history),
                        voice=config.get("tts_enabled", False),
                    )
                    print("\nAgent AI >")
                    print(render_markdown_terminal(reply))
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
                    print("\nInterrupted.")
                except Exception as e:
                    print(f"\n[ERROR] {e}")
            continue

        if command_low in ("/normal", "/chat"):
            agent_mode = False
            print(f"{GRAY}[Agent mode disabled. Returning to normal AI.]{RESET}")
            continue

        # Text-command alternative to the Ctrl+O keybinding: some terminals
        # never pass Ctrl+O through to the app at all, so this is a reliable
        # fallback. Checked here (before the agent_mode split below) so it
        # works identically in both /agent and normal chat.
        if command_low in ("/expand", "/details on", "/view extended", "/view expanded"):
            display_state.set_expanded(True)
            print(f"{GRAY}[Tool/Reasoning details: EXPANDED]{RESET}")
            continue

        if command_low in ("/collapse", "/details off", "/view collapsed", "/view compact"):
            display_state.set_expanded(False)
            print(f"{GRAY}[Tool/Reasoning details: COLLAPSED]{RESET}")
            continue
        if command_low in ("/notify true",):
            config = _update_config(notify=True)
        if command_low in ("/notify false",):
            config = _update_config(notify=False)

        if agent_mode:
            try:
                print("\n[Agent Thinking]")
                reply = ask_agent(
                    user_input,
                    history=list(agent_history),
                    voice=config.get("tts_enabled", False),
                )
            except KeyboardInterrupt:
                print("\nInterrupted.")
                continue
            except Exception as e:
                print(f"\n[ERROR] {e}")
                continue

            print("\nAgent AI >")
            print(render_markdown_terminal(reply))
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

        log_write(f"\nUser > {user_input}")
        print("\n[Thinking]")

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
            print("\nInterrupted.")
            continue

        except Exception as e:
            print(f"\n[ERROR] {e}")
            continue

        if config.get("tts_enabled") and HAS_STT:
            print("\nAI (Voice) >")
            log_write(f"\nAI (Voice) > {reply}")
        else:
            print("\nAI >")
            log_write(f"\nAI > {reply}")

        print(render_markdown_terminal(reply))
        if config.get("tts_enabled") and HAS_STT:
            speak(reply, block=True)

        # Close the chunk with the final reply, then trigger background summarization.
        _cm.close_chunk(reply)
        _cm.maybe_summarize_async()

if __name__ == "__main__":
    chat_loop()
