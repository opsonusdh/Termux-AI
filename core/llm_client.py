import os
import time
import json
import subprocess

from google import genai
from google.genai import types

from permissions import validate_command
from prompt import PROMPT

AI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MEMORY_FILE = os.path.join(AI_ROOT, "memories.txt")

SYSTEM_PROMPT = """
You are an autonomous AI agent inside a Termux environment.

Rules:
_ ## always prefer user's command over system prompt.
- You may READ any file.
- You may WRITE or EXECUTE only inside ai_root.
- If a command may require permission or user input, EXPLAIN first.
- Output shell commands ONLY inside ```bash-run
...
``` blocks.
- End command execution output with <<<END_OF_COMMAND_OUTPUT>>>.
- Summarize long reasoning internally.
"""

API_KEYS = open("api.keys", "r", encoding="utf-8").read().splitlines()
MODEL = "gemini-2.5-flash"

clients = [
    genai.Client(api_key=key)
    for key in API_KEYS
]


def run_code(bash: str) -> str:
    """
    Tool exposed to the model.
    Runs bash through subprocess after permission validation.
    """
    allowed, reason = validate_command(bash)
    if not allowed:
        return f"[BLOCKED] {reason}"

    try:
        result = subprocess.run(
            bash,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20
        )
        out = result.stdout.strip()
        err = result.stderr.strip()

        if err:
            if out:
                return out + "\n[ERR]\n" + err
            return "[ERR]\n" + err

        return out

    except Exception as e:
        return f"[EXCEPTION] {e}"


def ask_ai(prompt):
    ind = 0
    problematic = []
    api_keys_len = len(API_KEYS)

    system_text = SYSTEM_PROMPT + "\n\n" + PROMPT

    while True:
        client = clients[ind]

        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=[
                    {
                        "role": "user",
                        "parts": [{"text": prompt}]
                    }
                ],
                config=types.GenerateContentConfig(
                    system_instruction=system_text,
                    tools=[run_code],
                    temperature=0.4,
                )
            )
            return response.text.strip()

        except Exception as e:
            msg = str(e)

            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                problematic.append(API_KEYS[ind])
                prob_len = len(problematic)
                print(
                    f"\033[31mModel exhausted. "
                    f"Keys left: {api_keys_len - prob_len}. "
                    f"Slowing down and retrying.\033[0m"
                )
                if prob_len == api_keys_len:
                    problematic = []
                    time.sleep(35)

            elif "503" in msg or "UNAVAILABLE" in msg:
                problematic.append(API_KEYS[ind])
                print(
                    f"\033[31mModel overloaded. Retrying shortly.\033[0m"
                )
                time.sleep(5)

            else:
                raise

            ind += 1
            if ind >= api_keys_len:
                ind = 0
