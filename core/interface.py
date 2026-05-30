import os
import json
import sys
import subprocess
from concurrent.futures import ThreadPoolExecutor
from llm_client import ask_ai
from renderer import render_markdown_terminal, GRAY, RESET
from tools import *

sys.dont_write_bytecode = True

# Start sys diagnosis in background immediately
_diag_executor = ThreadPoolExecutor(max_workers=1)
_diag_future   = _diag_executor.submit(run_diagnosis)

# Add Termux-STT to path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DEFAULT_CONFIG = {
    "stt_path": os.path.join(BASE_DIR, "Termux-STT"),
    "tts_enabled": False,
    "use_groq": False
}
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

    if subprocess.run(
        "which edge-tts",
        shell=True,
        capture_output=True
    ).returncode != 0:
        raise Exception("edge-tts not found")
    if subprocess.run(
        "which mpv",
        shell=True,
        capture_output=True
    ).returncode != 0:
        raise Exception("mpv not found")

    HAS_STT = True

except Exception:
    HAS_STT = False


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
    try:
        whatsapp_manager.start()
    except Exception as e:
        print(f"⚠️ Failed to start WhatsApp Manager: {e}")

    history: list[dict] = []
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
            history.append({"role": "user",      "content": greeting_prompt})
            history.append({"role": "assistant",  "content": reply})
        except:
            pass
    else:
        print()

    while True:
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

        print("\n[Thinking]")

        # Inject diagnosis on first user message if not already done at greeting
        call_history = list(history)
        if not _diag_injected:
            diag_msg = _get_diag_history()
            if diag_msg:
                call_history = [diag_msg] + call_history
                _diag_injected = True

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
        else:
            print("\nAI >")

        print(render_markdown_terminal(reply))
        if config.get("tts_enabled") and HAS_STT:
            speak(reply, block=True)

        history.append({"role": "user",     "content": user_input})
        history.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    chat_loop()
