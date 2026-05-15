import os
import json
import sys
from llm_client import ask_ai
from renderer import render_markdown_terminal, GRAY, RESET
from tools import speak

sys.dont_write_bytecode = True

# Add Termux-STT to path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DEFAULT_CONFIG = {
    "stt_path": os.path.join(BASE_DIR, "Termux-STT"),
    "tts_enabled": False,
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
    HAS_STT = True
except ImportError:
    HAS_STT = False

def chat_loop():
    context = ""

    print("Terminal AI ready. Type 'exit' to quit.")
    if HAS_STT:
        if not config.get("tts_enabled"):
            print("""Enter "start voice" to use Voice Input.""")
    
    if HAS_STT and config.get("tts_enabled"):
        try:
            print("\nAI > ")
            reply = ask_ai(
                "System: Start the conversation naturally like a friendly assistant. "
                "Avoid robotic introductions, capability lists, or mentioning tools unless asked. "
                "Keep the tone warm and casual."
            )
            print(render_markdown_terminal(reply))
            speak(reply)
            context += f"System: Greet the user.\nAI:\n{reply}\n"
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
                user_input = listen(once=True)
                if user_input:
                    print(f"\nYOU (Voice) > {user_input}")
                else:
                    print(f"{GRAY}[No speech detected]{RESET}")
                    continue
            except Exception as e:
                print(f"\n[STT ERROR] {e}")
                continue
        
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "exit.", "quit."):
            print("Session ended.")
            break
        if user_input.lower() in ["start voice", "start voice"]:
            config["tts_enabled"] = True
            with open(CONFIG_PATH, "w") as f:
                 json.dump(config, f, indent=4)
            continue
        if user_input.lower() in ("stop voice", "stop voice."):
            config["tts_enabled"] = False
            with open(CONFIG_PATH, "w") as f:
                 json.dump(config, f, indent=4)
            continue

        context += f"\nUser:\n{user_input}\n"

        print("\n[Thinking]")

        try:
            reply = ask_ai(context)

        except KeyboardInterrupt:
            print("\nInterrupted.")
            continue

        except Exception as e:
            print(f"\n[ERROR] {e}")
            continue

        print("\nAI >")

        print(
            render_markdown_terminal(reply)
        )
        if config.get("tts_enabled"):
            speak(reply)

        context += f"\nAI:\n{reply}\n"


if __name__ == "__main__":
    chat_loop()
