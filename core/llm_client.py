import os
import time
import json
from openai import OpenAI

from prompt import SYSTEM_PROMPT
from renderer import RED, GRAY, RESET
from tools import *

AI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

API_KEYS = [
    k.strip()
    for k in open(
        os.path.join(AI_ROOT, "api.keys"),
        "r",
        encoding="utf-8"
    ).read().splitlines()
    if k.strip()
]

# WARNING: DON'T CHANGE THE MODELS VARIABLE UNLESS EXPLICITLY ASKED FOR
MODELS = [
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]

def make_client(key):
    return OpenAI(
        api_key=key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )


def ask_ai(prompt: str) -> str:
    ind       = 0
    model_ind = 0
    problematic: list = []
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
        client = make_client(API_KEYS[ind])

        try:
            response = client.chat.completions.create(
                model=MODELS[model_ind],
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
                        result = run_code(
                            bash=args.get("bash", ""),
                            timeout=int(args.get("timeout", 0)),
                        )

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

                    elif tool_name == "read_file":
                        result = read_file(
                            path=args.get("path", ""),
                            segment_start=args.get("segment_start"),
                            segment_end=args.get("segment_end"),
                            unit=args.get("unit", "lines"),
                        )

                    elif tool_name == "write_file":
                        result = write_file(
                            path=args.get("path", ""),
                            content=args.get("content", ""),
                            mode=args.get("mode", "overwrite"),
                            segment_start=args.get("segment_start"),
                            segment_end=args.get("segment_end"),
                            unit=args.get("unit", "lines"),
                        )

                    elif tool_name == "index_files":
                        result = index_files(
                            path=args.get("path", ""),
                            extension_filter=args.get("extension_filter", ""),
                        )

                    elif tool_name == "web_scrape":
                        result = web_scrape(
                            url=args.get("url", ""),
                            selector=args.get("selector", None),
                        )
                    elif tool_name == "sleep_mode":
                        result = sleep_mode()

                    else:
                        print(f"{RED}[ERROR] Unknown tool: {tool_name}{RESET}")
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

            if (
                "429" in msg_str
                or "RESOURCE_EXHAUSTED" in msg_str
                or "rate limit" in msg_str.lower()
            ):
                problematic.append(API_KEYS[ind])
                prob_len = len(problematic)
                print(
                    f"{RED}Model exhausted. "
                    f"Keys left: {api_keys_len - prob_len}. "
                    f"Slowing down and retrying.{RESET}"
                )
                if prob_len == api_keys_len:
                    problematic = []
                    if model_ind >= len(MODELS) - 1:
                        model_ind = 0
                        time.sleep(35)
                    else:
                        model_ind += 1
                        time.sleep(5)
                        
            elif (
                "503" in msg_str
                or "UNAVAILABLE" in msg_str
                or "overloaded" in msg_str.lower()
            ):
                problematic.append(API_KEYS[ind])
                print(f"{RED}Model overloaded. Retrying shortly.{RESET}")
                time.sleep(5)
            
            elif "API_KEY_INVALID" in msg_str:
                print(f"{RED}Invalid API key{RESET}")
            else:
                raise

            ind += 1
            if ind >= api_keys_len:
                ind = 0
