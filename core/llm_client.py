import os
import time
import json
from openai import OpenAI

from prompt import SYSTEM_PROMPT
from memory_store import build_memory_block
from renderer import RED, GRAY, RESET
from tools import *

AI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

API_KEYS = open(
    os.path.join(AI_ROOT, "api.keys"), "r", encoding="utf-8"
).read().splitlines()

MODEL = "gemini-1.5-flash"

clients = [
    OpenAI(
        api_key=key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    for key in API_KEYS
]


def ask_ai(prompt: str) -> str:
    ind = 0
    problematic = set()
    api_keys_len = len(API_KEYS)

    #  READ: retrieve and inject memories 
    memory_block = build_memory_block(prompt)

    system_content = SYSTEM_PROMPT
    if memory_block:
        system_content = memory_block + "\n\n" + SYSTEM_PROMPT

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user",   "content": prompt},
    ]

    #  Inference loop 
    while True:
        client = clients[ind]

        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS_DESCRIPTION,
                tool_choice="auto",
            )

            msg = response.choices[0].message

            if msg.tool_calls:
                messages.append(msg)

                for tool_call in msg.tool_calls:
                    tool_name = tool_call.function.name

                    try:
                        args = json.loads(tool_call.function.arguments or "{}")
                    except Exception:
                        args = {}

                    #  Dispatch tools 
                    if tool_name == "run_code":
                        result = run_code(bash=args.get("bash", ""))

                    elif tool_name == "save_memory":
                        result = save_memory(
                            text=args.get("text", ""),
                            type_=args.get("type_", "fact"),
                            tags=args.get("tags", ""),
                            priority=int(args.get("priority", 7)),
                        )
                    elif tool_name == "retrieve_memory":
                        result = retrieve_memory(
                            query=args.get("query", ""),
                            top_k=int(args.get("top_k", 5)),
                        )
                    elif tool_name == "web_scrape":
                        result = web_scrape(
                            url=args.get("url", ""), 
                            selector=args.get("selector", None)
                        )
                    elif tool_name == "index_files":
                        result = index_files(
                            path=args.get("path", ""), 
                            extension_filter=args.get("extension_filter", "")
                        )

                    else:
                        print(f"{RED}[ERROR] Trying to use Unknown tool: {tool_name}{RESET}")
                        result = f"[ERROR] Unknown tool: {tool_name}"

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    })

                continue

            if msg.content:
                return msg.content.strip()

            return "[EMPTY RESPONSE]"

        except Exception as e:
            msg_str = str(e)

            # Detect rate limits or temporary outages
            is_rate_limit = any(x in msg_str for x in ["429", "RESOURCE_EXHAUSTED", "rate limit"])
            is_overload = any(x in msg_str.lower() for x in ["503", "unavailable", "overloaded"])

            if is_rate_limit or is_overload:
                problematic.add(ind)
                prob_len = len(problematic)
                keys_left = api_keys_len - prob_len
                
                status_msg = "Exhausted" if is_rate_limit else "Overloaded"
                print(f"{RED}Model {status_msg}. Keys left: {max(0, keys_left)}.{RESET}")

                if prob_len >= api_keys_len:
                    print(f"{RED}All keys exhausted. Cooling down for 35s...{RESET}")
                    problematic.clear()
                    time.sleep(35)
                else:
                    time.sleep(2)
            else:
                # Critical error, re-raise
                raise

            # Move to next key
            ind = (ind + 1) % api_keys_len
