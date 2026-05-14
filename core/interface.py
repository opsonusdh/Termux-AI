from llm_client import ask_ai
from renderer import render_markdown_terminal


def chat_loop():
    context = ""

    print("Terminal AI ready. Type 'exit' to quit.")

    while True:
        user_input = input("\n\nYOU > ").strip()

        if user_input.lower() in ("exit", "quit"):
            print("Session ended.")
            break

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

        context += f"\nAI:\n{reply}\n"


if __name__ == "__main__":
    chat_loop()