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
from renderer import render_markdown_terminal, GRAY, RESET, RED
from tools import *
import context_manager as _cm

# Config
BASE_DIR    = _ROOT
CONFIG_PATH = paths.CONFIG_FILE
DEFAULT_CONFIG = {
    "stt_path":    os.path.join(BASE_DIR, "Termux-STT"),
    "tts_enabled": False,
    "use_groq":    False,
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

STT_PATH = os.path.expanduser(config["stt_path"])
if STT_PATH not in sys.path:
    sys.path.append(STT_PATH)

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
                user_input = input("\nYOU > ").strip()
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
                config["tts_enabled"] = False
                with open(CONFIG_PATH, "w") as f:
                    json.dump(config, f, indent=4)
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
            config["tts_enabled"] = True
            with open(CONFIG_PATH, "w") as f:
                 json.dump(config, f, indent=4)
            continue
        if user_input.lower() in ["start voice local.", "start voice local"]:
            config["tts_enabled"] = True
            config["use_groq"] = False
            with open(CONFIG_PATH, "w") as f:
                 json.dump(config, f, indent=4)
            continue
        if user_input.lower() in ["start voice remote.", "start voice remote"]:
             config["tts_enabled"] = True
             config["use_groq"] = True
             with open(CONFIG_PATH, "w") as f:
                  json.dump(config, f, indent=4)
             continue
        if user_input.lower() in ("stop voice.", "stop voice"):
            config["tts_enabled"] = False
            with open(CONFIG_PATH, "w") as f:
                 json.dump(config, f, indent=4)
            continue

        command = user_input.strip()
        command_low = command.casefold()

        # Legacy project runner: /agent auto
        if command_low in ("/agent auto", "agent auto"):
            import llm_client
            while True:
                result = llm_client.run_agent_step(voice=config.get("tts_enabled", False))
                print(render_markdown_terminal(f"**Agent Status:** {result}"))
                if "No pending" in result or "failed" in result.lower():
                    break
                time.sleep(1)
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
                except KeyboardInterrupt:
                    print("\nInterrupted.")
                except Exception as e:
                    print(f"\n[ERROR] {e}")
            continue

        if command_low in ("/normal", "/chat"):
            agent_mode = False
            print(f"{GRAY}[Agent mode disabled. Returning to normal AI.]{RESET}")
            continue

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
            continue

        log_write(f"\nUser > {user_input}")
        print("\n[Thinking]")

        # Inject diagnosis on first user message if not already done at greeting
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
