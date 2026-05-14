import os
import time
import json
import subprocess

from openai import OpenAI

from permissions import validate_command
from prompt import PROMPT
from renderer import RED, GRAY, RESET
import memory_store

AI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

API_KEYS = open(
    os.path.join(AI_ROOT, "api.keys"), "r", encoding="utf-8"
).read().splitlines()

MODEL = "gemini-3-flash-preview"

clients = [
    OpenAI(
        api_key=key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    for key in API_KEYS
]


#  Tools 

def run_code(bash: str) -> str:
    """Execute shell commands in Termux after permission validation."""
    allowed, reason = validate_command(bash)
    if not allowed:
        return f"[BLOCKED] {reason}"

    try:
        print(f"{GRAY}[EXECUTING] {bash}{RESET}")

        result = subprocess.run(
            bash,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )

        out = result.stdout.strip()
        err = result.stderr.strip()

        if err:
            if out:
                print(
                    f"{GRAY}[OUT]\n"
                    + out
                    + f"\n{RED}[ERR]\n"
                    + err
                    + f"{RESET}"
                )
                return out + "\n[ERR]\n" + err
            print(f"{RED}[ERR]\n{err}{RESET}")
            return "[ERR]\n" + err

        print(f"{GRAY}[OUT]\n{out}{RESET}")
        return out

    except Exception as e:
        print(f"{RED}[EXCEPTION]\n{e}{RESET}")
        return f"[EXCEPTION] {e}"


def save_memory(text: str, type_: str, tags: str, priority: int) -> str:
    """
    Persist a structured memory entry to memories.txt.
    Called by the model when it learns something stable.
    """
    result = memory_store.save_memory(
        text=text,
        type_=type_,
        tags=tags,
        priority=priority,
    )
    if result.startswith("[ERROR"):
        print(f"{RED}[MEMORY SAVE FAILED] {result}{RESET}")
        return result

    print(f"{GRAY}[MEMORY SAVED] {result}{RESET}")
    return f"Memory saved: {result}"


def retrieve_memory(query: str, top_k: int = 5) -> str:
    """
    Retrieve relevant memories and return them as formatted text.
    Also prints a grey debug line with the query keywords.
    """
    keywords = sorted(memory_store._tokenize(query))
    keyword_str = ", ".join(keywords) if keywords else "(none)"

    print(f"{GRAY}[MEMORY] retrieving for keywords: {keyword_str}{RESET}")

    hits = memory_store.retrieve(query, top_k=top_k)

    if not hits:
        return "No relevant memories found."

    lines = []
    for entry in hits:
        lines.append(f"[{entry.type}] {entry.text}")

    return "\n".join(lines)
    
    
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_code",
            "description": (
                "Execute shell commands inside the Termux environment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "bash": {
                        "type": "string",
                        "description": "Shell command to execute",
                    }
                },
                "required": ["bash"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": (
                "Persist a stable fact, preference, or instruction to long-term memory. "
                "Use this when you learn something the user will want you to remember across sessions. "
                "Do NOT use for temporary or one-off information."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The fact or preference to remember (one clear sentence).",
                    },
                    "type_": {
                        "type": "string",
                        "enum": ["preference", "instruction", "project", "fact", "workflow"],
                        "description": (
                            "Category: 'preference' for user habits/style, "
                            "'instruction' for behavioral rules, "
                            "'project' for project structure/paths, "
                            "'fact' for environment details, "
                            "'workflow' for recurring task patterns."
                        ),
                    },
                    "tags": {
                        "type": "string",
                        "description": (
                            "Comma-separated lowercase keywords that describe the memory "
                            "(e.g. 'shell,help,flag'). Used for retrieval."
                        ),
                    },
                    "priority": {
                        "type": "integer",
                        "description": (
                            "Importance 1–10. Use 10 for critical behavioral rules "
                            "(like shutdown instructions), 7–9 for strong preferences, "
                            "5–6 for useful facts."
                        ),
                    },
                },
                "required": ["text", "type_", "tags", "priority"],
            },
        },
    },
    { 
        "type": "function",
        "function": {
            "name": "retrieve_memory",
            "description": (
                "Search long-term memory for relevant stored facts, "
                "preferences, instructions, workflows, or project details. "
                "Use this when additional context from past interactions "
                "may help answer the user's request."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Natural language search query describing "
                            "what memory to retrieve."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": (
                            "Maximum number of relevant memories to return."
                        ),
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
]


#  Core inference 

def ask_ai(prompt: str) -> str:
    ind = 0
    problematic = []
    api_keys_len = len(API_KEYS)

    #  READ: retrieve and inject memories 
    memory_block = memory_store.build_memory_block(prompt)

    system_content = PROMPT
    if memory_block:
        system_content = memory_block + "\n\n" + PROMPT

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
                tools=TOOLS,
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
                    time.sleep(35)

            elif (
                "503" in msg_str
                or "UNAVAILABLE" in msg_str
                or "overloaded" in msg_str.lower()
            ):
                problematic.append(API_KEYS[ind])
                print(f"{RED}Model overloaded. Retrying shortly.{RESET}")
                time.sleep(5)

            else:
                raise

            ind += 1
            if ind >= api_keys_len:
                ind = 0
