import re
from llm_client import ask_ai
from executor import run_commands

END_MARKER = "<<<END_OF_COMMAND_OUTPUT>>>"

BASH_BLOCK_RE = re.compile(
    r"```bash-run\s*(.*?)```",
    re.DOTALL | re.IGNORECASE
)


def extract_commands(text):
    blocks = BASH_BLOCK_RE.findall(text)
    cleaned = BASH_BLOCK_RE.sub("", text).strip()
    return blocks, cleaned


def chat_loop():
    context = ""

    print("Terminal AI ready. Type 'exit' to quit.\n")

    while True:
        user_input = input("YOU > ").strip()
        if user_input.lower() in ("exit", "quit"):
            print("Session ended.")
            break

        context += f"\nUser:\n{user_input}\n"

        step = 0
        while True:
            step += 1
            print(f"\n[Thinking({step})]")

            reply = ask_ai(context)
            blocks, text_reply = extract_commands(reply)

            if text_reply:
                print("\nAI >")
                print(text_reply)

            if not blocks:
                context += "\nAI:\n" + reply + "\n"
                break

            for block in blocks:
                print("\n[Executing commands]")
                output = run_commands(block)
                print(output)

                context += (
                    "\nAI issued commands:\n"
                    + block
                    + "\nCommand output:\n"
                    + output
                    + "\n"
                )

                if END_MARKER not in output:
                    print("[Warning] Missing END marker. Stopping.")
                    return


if __name__ == "__main__":
    chat_loop()
