import os
import re
import sys
import json
import html
import time
import shlex
import signal
import heapq
import requests
import threading
import subprocess
import base64
from pathlib import Path
from openai import OpenAI
from urllib.parse import urljoin, urlparse
from datetime import datetime
from collections import defaultdict
from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from concurrent.futures import ThreadPoolExecutor, as_completed

# Path bootstrap
_CORE_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_CORE_DIR)
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)
if _ROOT_DIR not in sys.path:
    sys.path.insert(1, _ROOT_DIR)

from permissions import validate_command
from renderer import RED, GRAY, RESET, render_for_voice, render_markdown_terminal
import paths

# Import WhatsApp Manager (same package, safe relative import)
try:
    from whatsapp_manager import whatsapp_manager
    WP_AVAILABLE = True
except (ImportError, FileNotFoundError):
    try:
        sys.path.append(_CORE_DIR)
        from whatsapp_manager import whatsapp_manager
        WP_AVAILABLE = True
    except FileNotFoundError:
        WP_AVAILABLE = False

WAKE_WORDS = ["orion", "orien", "orian"]
PRINT_LINE_THRESHOLD = 20
PRINT_CHAR_THRESHOLD = 500
AI_ROOT = _ROOT_DIR
DIAGNOSIS_TIMEOUT = 10

_speak_thread: threading.Thread | None = None

# ── TOOL DESCRIPTIONS ──────────────────────────────────────────────────────────

TOOLS_DESCRIPTION = [
    {
        "type": "function",
        "function": {
            "name": "run_code",
            "description": (
                "Execute shell commands inside the system environment (Termux, Linux, or Windows). "
                "Supports an optional execution timeout in seconds."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "bash": {
                        "type": "string",
                        "description": "Shell command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Maximum execution time in seconds (0 = no limit).",
                        "default": 0,
                        "minimum": 0,
                    },
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
                "Persist a stable fact, preference, or instruction to long-term memory "
                "(memories.txt). Use when you learn something the user will want remembered "
                "across sessions. Do NOT use for raw code, logs, or temporary information — "
                "use index_files for bulk content."
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
                            "'preference' for user habits/style, "
                            "'instruction' for behavioral rules, "
                            "'project' for structure/paths, "
                            "'fact' for environment details, "
                            "'workflow' for recurring task patterns."
                        ),
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated lowercase keywords (e.g. 'shell,help,flag').",
                    },
                    "priority": {
                        "type": "integer",
                        "description": (
                            "Importance 1-10. Use 10 for critical behavioral rules, "
                            "7-9 for strong preferences, 5-6 for useful facts."
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
                "Search long-term memory for relevant stored facts, preferences, "
                "instructions, workflows, or project details. Also searches indexed "
                "code/doc chunks when relevant. Use before starting any non-trivial task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language description of what to recall.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of results to return.",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of a file without using the shell. "
                "Optionally read only a segment by specifying start and end "
                "positions as line numbers (1-indexed, inclusive) or byte offsets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or ~ path to the file.",
                    },
                    "segment_start": {
                        "type": "integer",
                        "description": (
                            "Start of the segment to read. "
                            "Line number (1-indexed) when unit='lines'; "
                            "byte offset when unit='bytes'. "
                            "Omit to read from the beginning."
                        ),
                    },
                    "segment_end": {
                        "type": "integer",
                        "description": (
                            "End of the segment to read (inclusive). "
                            "Line number when unit='lines'; byte offset when unit='bytes'. "
                            "Omit to read to the end of the file."
                        ),
                    },
                    "unit": {
                        "type": "string",
                        "enum": ["lines", "bytes"],
                        "description": "Whether segment_start/end are line numbers or byte offsets. Default: 'lines'.",
                        "default": "lines",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write content to a file without using the shell. "
                "Supports four modes: "
                "'overwrite' replaces the entire file, "
                "'append' adds content to the end, "
                "'prepend' inserts content at the start, "
                "'segment' replaces only the specified line range or byte range."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or ~ path to the file. Parent directories are created if missing.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to write.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append", "prepend", "segment"],
                        "description": (
                            "Write mode. "
                            "'overwrite': replace entire file (default). "
                            "'append': add to end. "
                            "'prepend': insert at start. "
                            "'segment': replace lines segment_start..segment_end with content."
                        ),
                        "default": "overwrite",
                    },
                    "segment_start": {
                        "type": "integer",
                        "description": (
                            "First line (1-indexed) or byte offset to replace. "
                            "Required when mode='segment'."
                        ),
                    },
                    "segment_end": {
                        "type": "integer",
                        "description": (
                            "Last line (inclusive) or byte offset to replace. "
                            "Required when mode='segment'. "
                            "Use the same value as segment_start to replace a single line."
                        ),
                    },
                    "unit": {
                        "type": "string",
                        "enum": ["lines", "bytes"],
                        "description": "Whether segment_start/end are line numbers or byte offsets. Default: 'lines'.",
                        "default": "lines",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "index_files",
            "description": (
                "Scan a directory (or single file) and index its contents into "
                "indexed_memory.txt for RAG retrieval. Use to learn about a codebase "
                "or set of documents. Never pollutes memories.txt."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the directory or file to index.",
                    },
                    "extension_filter": {
                        "type": "string",
                        "description": "Comma-separated extensions (e.g., '.py,.md'). Empty = all text files.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_scrape",
            "description": (
                "Fetch a URL and convert readable content into structured markdown. "
                "Supports HTML pages, plain text, JSON, tables, ordered/unordered lists, "
                "links, images, and media URLs. Optionally target one or more elements "
                "with a CSS selector."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the webpage to scrape.",
                    },
                    "selector": {
                        "type": "string",
                        "description": "Optional CSS selector to filter content (e.g., 'main', 'article', '.content').",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters to return after extraction. Default 12000, max 50000.",
                        "default": 12000,
                        "minimum": 1000,
                        "maximum": 50000,
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sleep_mode",
            "description": (
                "Put the assistant into passive sleep mode. "
                "Only Whisper speech detection remains active. "
                f"Continuously listens for the wake word '{WAKE_WORDS}'. "
                "When the wake word is detected, a lightweight AI relevance "
                "check determines whether the speaker is actually addressing "
                "the assistant."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "intermediate_print",
            "description": (
                "Print a status message or reasoning update to the terminal mid-task. "
                "Use this to communicate what you are currently doing before a result is ready."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The message to display. Markdown is supported.",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_whatsapp_message",
            "description": "Send a WhatsApp message to a specific phone number or contact ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to_phone": {
                        "type": "string",
                        "description": "The destination phone number or contact ID.",
                    },
                    "message_text": {
                        "type": "string",
                        "description": "The text content of the message to send.",
                    },
                },
                "required": ["to_phone", "message_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_whatsapp_status",
            "description": "Get the current status of the WhatsApp bot client and see if there are any pending received messages.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_whatsapp_chats",
            "description": "List all WhatsApp chats and groups with their names, JIDs, unread counts, and metadata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter_type": {
                        "type": "string",
                        "enum": ["all", "dm", "group"],
                        "description": "Filter results: 'all' returns everything, 'dm' returns only direct messages, 'group' returns only groups. Default is 'all'.",
                        "default": "all",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "silence_whatsapp_contact",
            "description": "Silence auto-replies to a specific contact or group for a given number of hours.",
            "parameters": {
                "type": "object",
                "properties": {
                    "jid":   {"type": "string", "description": "WhatsApp JID of the contact or group to silence."},
                    "hours": {"type": "number",  "description": "How many hours to silence (default 24). Pass 0 to lift immediately.", "default": 24},
                },
                "required": ["jid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "react_to_whatsapp_message",
            "description": "React to a specific WhatsApp message with an emoji (e.g. 👍 ❤️ 😂).",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "The serialized message ID to react to."},
                    "emoji":      {"type": "string", "description": "The emoji to react with."},
                },
                "required": ["message_id", "emoji"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_whatsapp_contact_info",
            "description": "Fetch profile information for a WhatsApp contact.",
            "parameters": {
                "type": "object",
                "properties": {
                    "jid": {"type": "string", "description": "WhatsApp JID of the contact."},
                },
                "required": ["jid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_whatsapp_group_participants",
            "description": "List all participants in a WhatsApp group along with their roles.",
            "parameters": {
                "type": "object",
                "properties": {
                    "jid": {"type": "string", "description": "Group JID (ends in @g.us)."},
                },
                "required": ["jid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_whatsapp_media",
            "description": "Download the media file from a WhatsApp message.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "The serialized message ID of the media message."},
                },
                "required": ["message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_whatsapp_message",
            "description": "Schedule a WhatsApp message to be sent automatically at a specific future time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to":      {"type": "string", "description": "Recipient JID or phone number."},
                    "message": {"type": "string", "description": "The message text to send."},
                    "send_at": {"type": "string", "description": "ISO 8601 datetime string for when to send."},
                },
                "required": ["to", "message", "send_at"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_whatsapp_chat",
            "description": "Search for messages containing a keyword or phrase within a specific WhatsApp chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "jid":   {"type": "string", "description": "Chat JID to search in."},
                    "query": {"type": "string", "description": "The keyword or phrase to search for."},
                    "limit": {"type": "integer", "description": "Max results to return (default 20).", "default": 20},
                },
                "required": ["jid", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "archive_whatsapp_chat",
            "description": "Archive or unarchive a WhatsApp chat to reduce clutter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "jid":     {"type": "string",  "description": "Chat JID to archive/unarchive."},
                    "archive": {"type": "boolean", "description": "True to archive, False to unarchive. Default is True.", "default": True},
                },
                "required": ["jid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_whatsapp_seen",
            "description": "Mark a WhatsApp chat as read.",
            "parameters": {
                "type": "object",
                "properties": {
                    "jid": {"type": "string", "description": "Chat JID to mark as read."},
                },
                "required": ["jid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pending_whatsapp_messages",
            "description": "Retrieve and optionally clear any pending received WhatsApp messages from the background queue.",
            "parameters": {
                "type": "object",
                "properties": {
                    "clear": {
                        "type": "boolean",
                        "description": "Whether to clear the messages from the queue after retrieving them. Default is true.",
                        "default": True,
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_whatsapp_chat_history",
            "description": "Fetch the recent chat message history timeline for a specific phone number or contact ID from WhatsApp.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to_phone": {
                        "type": "string",
                        "description": "The phone number or contact ID to fetch chat history for.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "The maximum number of recent messages to fetch. Default is 5.",
                        "default": 5,
                    },
                },
                "required": ["to_phone"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_whatsapp_busy_mode",
            "description": "Enable or disable auto-reply 'busy' mode with a specific instruction and optional group exclusions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "enabled": {
                        "type": "boolean",
                        "description": "Whether busy mode should be enabled or disabled.",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "The instructions for generating auto-replies.",
                    },
                    "exclude_all_groups_except": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of group names or JIDs to include.",
                    },
                },
                "required": ["enabled"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_whatsapp_report",
            "description": "Get a full report of all WhatsApp messages received and sent during busy mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "clear": {
                        "type": "boolean",
                        "description": "Whether to clear the log after generating the report. Default is false.",
                        "default": False,
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_whatsapp_user_profile",
            "description": "Set personal context about the user that Orion will include in WhatsApp auto-replies.",
            "parameters": {
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "description": "A plain text description of the user.",
                    },
                },
                "required": ["profile"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "initialize_project",
            "description": "Initialize a new project and set the global goal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The name of the project."},
                    "goal": {"type": "string", "description": "The main goal of the project."}
                },
                "required": ["name", "goal"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_subtask",
            "description": "Add a new subtask to the active project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Description of the subtask."}
                },
                "required": ["description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_subtask",
            "description": "Update status, notes, or verification of an existing subtask.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "The 1-based ID of the subtask to update."},
                    "status": {"type": "string", "enum": ["pending", "active", "completed", "failed"], "description": "New status for the subtask."},
                    "notes": {"type": "string", "description": "Progress notes for the subtask."},
                    "verification": {"type": "string", "description": "Verification steps or outputs."}
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve_chunk",
            "description": (
                "Retrieve the full raw conversation chunk or subchunk by its stable ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chunk_id": {
                        "type": "string",
                        "description": "The stable chunk ID to retrieve (e.g. '3' or '3.1')."
                    }
                },
                "required": ["chunk_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_chunks",
            "description": "List all stored conversation chunks with their IDs and one-line summaries.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_diagnosis",
            "description": "Run a system diagnosis to check battery, weather, storage, memory, network, and datetime info.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_subtask",
            "description": (
                "Delegate a subtask to a sub-AI model and receive its structured report back."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "A clear, imperative instruction for what the sub-AI should produce.",
                    },
                    "context": {
                        "type": "string",
                        "description": "All background information, raw data, or snippets needed.",
                        "default": "",
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional sub-AI model name.",
                        "default": "gemini-2.5-flash-lite",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Maximum tokens the sub-AI may generate.",
                        "default": 2048,
                        "minimum": 256,
                        "maximum": 8192,
                    },
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate an image from a text prompt using Google Gemini native image generation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Vivid text description of the desired image.",
                    },
                    "quality": {
                        "type": "string",
                        "enum": ["flash", "pro"],
                        "description": "Quality tier. Default 'flash'.",
                        "default": "flash",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Base filename without extension.",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
]

# ── LOGGING HELPERS ────────────────────────────────────────────────────────────

def log_write(msg: str) -> None:
    try:
        os.makedirs(paths.LOGS_DIR, exist_ok=True)
        with open(paths.HISTORY_FILE, "a", encoding="utf-8") as f:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def wa_log_write(direction: str, sender_id: str, sender_name: str, message: str) -> None:
    try:
        os.makedirs(paths.LOGS_DIR, exist_ok=True)
        wa_file = os.path.join(paths.LOGS_DIR, "whatsapp_log.jsonl")
        entry = {
            "timestamp": datetime.now().isoformat(),
            "direction": direction,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "message": message,
        }
        with open(wa_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── MEMORY / DELEGATION SUBSYSTEM ─────────────────────────────────────────────

_STOP_WORDS = {
    "a", "an", "the", "is", "it", "in", "on", "at", "to", "do", "be",
    "of", "and", "or", "for", "with", "that", "this", "i", "you", "we",
    "me", "my", "your", "how", "what", "when", "where", "can", "could",
    "would", "should", "will", "if", "then", "so", "are", "was", "were",
    "have", "has", "had", "not", "but", "from", "use", "get", "let",
    "run", "its", "just", "want", "need", "try", "also", "any", "some",
    "all", "no", "more", "about", "by", "up", "as", "into", "out", "now",
}

_CATEGORY_TREE = {
    "preference": ["shell", "ui", "style", "commands", "help", "flag", "output"],
    "instruction": ["shutdown", "process", "kill", "safety", "behavior", "close"],
    "project":     ["termux", "windows", "tui", "ai_root", "workspace", "repo", "code"],
    "fact":        ["environment", "device", "installed", "paths", "api", "key"],
    "workflow":    ["git", "python", "download", "script", "build", "install"],
}

_STRUCT_RE = re.compile(
    r"^\[(?P<type>\w+)\]\[(?P<tags>[^\]]*)\]\[(?P<priority>\d+)\]\s*(?P<text>.+)$"
)

_LEGACY_RE = re.compile(
    r"^(?:Learned|Note|Instruction|Tip|Fact|Preference):\s*(.+)$",
    re.IGNORECASE,
)

_INDEXED_TAG = "indexed"


def build_memory_block(prompt: str) -> str:
    memories = retrieve_memory(query=prompt, top_k=5)
    if not memories or "No relevant memory" in memories:
        return ""
    return f"## Long-Term Memory & Project Context\n{memories}"


def save_memory(text: str, type_: str = "fact", tags: str = "", priority: int = 7) -> str:
    entry = f"[{type_}][{tags}][{priority}] {text.strip()}\n"
    mem_file = paths.MEMORY_FILE
    os.makedirs(os.path.dirname(mem_file), exist_ok=True)
    with open(mem_file, "a", encoding="utf-8") as f:
        f.write(entry)
    return f"Saved memory: {entry.strip()}"


def retrieve_memory(query: str, top_k: int = 5) -> str:
    mem_file = paths.MEMORY_FILE
    if not os.path.exists(mem_file):
        return "No relevant memory found."

    with open(mem_file, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    if not lines:
        return "No relevant memory found."

    q_words = set(re.findall(r"\w+", query.lower())) - _STOP_WORDS
    results = []

    for line in lines:
        m = _STRUCT_RE.match(line)
        if m:
            text = m.group("text")
            priority = int(m.group("priority"))
        else:
            text = line
            priority = 5

        t_words = set(re.findall(r"\w+", text.lower())) - _STOP_WORDS
        overlap = len(q_words & t_words)
        if overlap > 0:
            score = overlap * 10 + priority
            results.append((score, text))

    if not results:
        # Return top priority items if no query match
        top_lines = lines[:top_k]
        return "\n".join(f"- {l}" for l in top_lines)

    results.sort(key=lambda x: x[0], reverse=True)
    top_res = results[:top_k]
    return "\n".join(f"- {text}" for score, text in top_res)


def read_file(path: str, segment_start: int | None = None, segment_end: int | None = None, unit: str = "lines") -> str:
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(paths.ROOT, p)

    if not os.path.exists(p):
        return f"[ERROR] File not found: {path}"

    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            if segment_start is None and segment_end is None:
                return f.read()

            if unit == "lines":
                lines = f.readlines()
                start = (segment_start - 1) if segment_start and segment_start > 0 else 0
                end = segment_end if segment_end and segment_end <= len(lines) else len(lines)
                return "".join(lines[start:end])
            else: # bytes
                f.seek(segment_start or 0)
                length = (segment_end - segment_start + 1) if (segment_start and segment_end) else None
                return f.read(length) if length else f.read()

    except Exception as e:
        return f"[ERROR] Failed to read file: {e}"


def write_file(path: str, content: str, mode: str = "overwrite", segment_start: int | None = None, segment_end: int | None = None, unit: str = "lines") -> str:
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(paths.ROOT, p)

    os.makedirs(os.path.dirname(p), exist_ok=True)

    try:
        if mode == "overwrite":
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Successfully wrote {len(content)} characters to {path}."

        elif mode == "append":
            with open(p, "a", encoding="utf-8") as f:
                f.write(content)
            return f"Successfully appended to {path}."

        elif mode == "prepend":
            existing = ""
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    existing = f.read()
            with open(p, "w", encoding="utf-8") as f:
                f.write(content + existing)
            return f"Successfully prepended to {path}."

        elif mode == "segment":
            if not os.path.exists(p):
                lines = []
            else:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()

            start = (segment_start - 1) if segment_start and segment_start > 0 else 0
            end = segment_end if segment_end else len(lines)

            new_lines = content.splitlines(keepends=True)
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"

            lines[start:end] = new_lines
            with open(p, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return f"Successfully updated lines {start+1}..{end} in {path}."

        else:
            return f"[ERROR] Invalid write mode: {mode}"

    except Exception as e:
        return f"[ERROR] Failed to write file: {e}"


def index_files(path: str, extension_filter: str = "") -> str:
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(paths.ROOT, p)

    if not os.path.exists(p):
        return f"[ERROR] Path not found: {path}"

    exts = [e.strip().lower() for e in extension_filter.split(",") if e.strip()] if extension_filter else []

    indexed_count = 0
    idx_file = paths.INDEXED_MEMORY_FILE

    files_to_index = []
    if os.path.isfile(p):
        files_to_index.append(p)
    else:
        for root, _, files in os.walk(p):
            for file in files:
                if exts:
                    if any(file.lower().endswith(e) for e in exts):
                        files_to_index.append(os.path.join(root, file))
                else:
                    files_to_index.append(os.path.join(root, file))

    with open(idx_file, "a", encoding="utf-8") as out:
        for filepath in files_to_index[:100]: # Cap at 100 files
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    rel_p = os.path.relpath(filepath, paths.ROOT)
                    out.write(f"--- FILE: {rel_p} ---\n{content}\n\n")
                    indexed_count += 1
            except Exception:
                pass

    return f"Successfully indexed {indexed_count} file(s) into indexed_memory.txt."


def web_scrape(url: str, selector: str | None = None, max_chars: int = 12000) -> str:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(url, headers=headers, timeout=12)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        for s in soup(["script", "style", "nav", "footer", "header"]):
            s.decompose()

        if selector:
            target = soup.select(selector)
            text = "\n".join(el.get_text(separator="\n", strip=True) for el in target)
        else:
            text = soup.get_text(separator="\n", strip=True)

        return text[:max_chars] if len(text) > max_chars else text
    except Exception as e:
        return f"[ERROR] Web scrape failed: {e}"


# ── SUB-AI DELEGATION ──────────────────────────────────────────────────────────

_DELEGATE_SYS_PROMPT = """\
You are a focused sub-assistant. Your sole job is to complete the specific task
given to you as accurately and concisely as possible.

Rules:
- Use available tools to gather information or perform actions if required for the task.
- Use ONLY the context provided or gathered via tools.
- Structure your output clearly so the supervising AI can parse it easily.
- If the task is impossible even with tool access, say so explicitly and explain why.
- Do not pad your response with pleasantries or meta-commentary.
"""

_DELEGATE_MODELS = [
    {"provider_id": "google",     "name": "gemini-2.5-flash-lite", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/"},
    {"provider_id": "openrouter", "name": "google/gemini-2.0-flash-001", "base_url": "https://openrouter.ai/api/v1"},
    {"provider_id": "nvidia",     "name": "nvidia/llama-3.1-nemotron-nano-8b-v1", "base_url": "https://integrate.api.nvidia.com/v1"},
    {"provider_id": "groq",       "name": "qwen/qwen3-32b", "base_url": "https://api.groq.com/openai/v1/"},
]


def _dispatch_sub_tool(name: str, args_raw: str) -> str:
    """Helper to execute tools on behalf of a delegated sub-AI."""
    try:
        args = json.loads(args_raw)
    except Exception:
        args = {}

    local_funcs = {
        "run_code": run_code,
        "read_file": read_file,
        "write_file": write_file,
        "save_memory": save_memory,
        "retrieve_memory": retrieve_memory,
        "index_files": index_files,
        "web_scrape": web_scrape,
        "generate_image": generate_image,
        "send_whatsapp_message": send_whatsapp_message,
        "get_whatsapp_status": get_whatsapp_status,
        "get_whatsapp_chats": get_whatsapp_chats,
        "get_pending_whatsapp_messages": get_pending_whatsapp_messages,
        "fetch_whatsapp_chat_history": fetch_whatsapp_chat_history,
        "set_whatsapp_busy_mode": set_whatsapp_busy_mode,
        "get_whatsapp_report": get_whatsapp_report,
        "set_whatsapp_user_profile": set_whatsapp_user_profile,
        "archive_whatsapp_chat": archive_whatsapp_chat,
        "search_whatsapp_chat": search_whatsapp_chat,
        "get_whatsapp_group_participants": get_whatsapp_group_participants,
        "get_whatsapp_contact_info": get_whatsapp_contact_info,
        "react_to_whatsapp_message": react_to_whatsapp_message,
        "download_whatsapp_media": download_whatsapp_media,
        "silence_whatsapp_contact": silence_whatsapp_contact,
        "schedule_whatsapp_message": schedule_whatsapp_message,
        "run_diagnosis": run_diagnosis,
        "sleep_mode": sleep_mode,
        "intermediate_print": intermediate_print,
    }

    if name in local_funcs:
        try:
            res = local_funcs[name](**args)
            if not isinstance(res, str):
                res = json.dumps(res, indent=2)
            return res
        except Exception as e:
            return f"[TOOL ERROR] Execution failed: {str(e)}"

    return f"[TOOL ERROR] Tool '{name}' is not supported in the sub-AI delegation environment."


def delegate_subtask(
    task: str,
    context: str = "",
    model: str = "gemini-2.5-flash-lite",
    max_tokens: int = 2048,
    system_prompt: str | None = None,
) -> str:
    log_write(f"[delegate_subtask] model:{model} max_tokens:{max_tokens} task:{task[:80]}")
    print(f"{GRAY}[DELEGATE] → {model} | {task[:72]}{'...' if len(task) > 72 else ''}{RESET}")

    max_tokens = max(256, min(8192, int(max_tokens)))

    user_message = f"## Task\n{task.strip()}"
    if context and context.strip():
        user_message += f"\n\n## Context\n{context.strip()}"

    def _infer_provider(m: str) -> tuple[str, str]:
        if m.startswith("openrouter") or "/" in m:
            return "openrouter", "https://openrouter.ai/api/v1"
        if m.startswith("gemini") or m.startswith("gemma"):
            return "google", "https://generativelanguage.googleapis.com/v1beta/openai/"
        if "llama" in m or "mixtral" in m or "qwen" in m or "gpt" in m:
            return "groq", "https://api.groq.com/openai/v1/"
        if "nvidia" in m or "nemotron" in m or "deepseek" in m:
            return "nvidia", "https://integrate.api.nvidia.com/v1"
        return "google", "https://generativelanguage.googleapis.com/v1beta/openai/"

    primary_pid, primary_url = _infer_provider(model)

    from llm_client import API_KEYS
    rotation: list[dict] = []
    for k in API_KEYS.get(primary_pid, []):
        rotation.append({"key": k, "model": model, "base_url": primary_url, "pid": primary_pid})
    for slot in _DELEGATE_MODELS:
        pid = slot["provider_id"]
        fb_model = slot["name"]
        if fb_model == model: continue
        for k in API_KEYS.get(pid, []):
            rotation.append({"key": k, "model": fb_model, "base_url": slot["base_url"], "pid": pid})

    if not rotation:
        return "[ERROR] No API keys configured for any sub-AI provider."

    ind = 0
    attempts = 0
    max_attempts = len(rotation) * 2

    sys_p = system_prompt if system_prompt is not None else _DELEGATE_SYS_PROMPT
    while attempts < max_attempts:
        cfg = rotation[ind]
        headers = {"HTTP-Referer": "https://github.com/opsonusdh/Termux-AI", "X-Title": "Termux-AI"} if cfg["pid"] == "openrouter" else None
        client = OpenAI(api_key=cfg["key"], base_url=cfg["base_url"], default_headers=headers)
        messages = [
            {"role": "system", "content": sys_p},
            {"role": "user",   "content": user_message},
        ]
        
        try:
            resp = client.chat.completions.create(
                model=cfg["model"],
                messages=messages,
                max_tokens=max_tokens,
            )
            content = resp.choices[0].message.content
            if content:
                header = (
                    f"[Sub-AI Report | model={cfg['model']} | "
                    f"task={task[:60].strip()}{'...' if len(task) > 60 else ''}]\n"
                    f"{'-' * 60}\n"
                )
                return header + content.strip()
            return "[delegate_subtask] Sub-AI returned an empty response."

        except Exception as e:
            err = str(e).upper()
            is_transient = any(x in err for x in ("429", "RESOURCE_EXHAUSTED", "RATE LIMIT", "503", "UNAVAILABLE", "OVERLOADED"))
            if is_transient:
                print(f"{RED}[ERROR][{cfg['pid']}/{cfg['model']}] Rate-limited/overloaded. Trying next...{RESET}")
                time.sleep(2)
            else:
                print(f"{RED}[ERROR][{cfg['pid']}/{cfg['model']}] Error: {str(e)[:120]}{RESET}")
            ind = (ind + 1) % len(rotation)
            attempts += 1

    return "[ERROR] All sub-AI providers and keys failed after multiple attempts."


# ── GENERATE IMAGE ─────────────────────────────────────────────────────────────

_NANO_BANANA_MODELS = [
    "gemini-3.1-flash-image-preview",
    "gemini-3-pro-image-preview",
    "imagen-4.0-generate-preview-05-20",
    "imagen-4.0-ultra-generate-exp-05-20",
    "imagen-3.0-generate-002",
    "imagen-3.0-generate-001",
]

_NANO_BANANA_QUALITY_ALIAS = {
    "flash": "gemini-3.1-flash-image-preview",
    "pro":   "gemini-3-pro-image-preview",
}

_NANO_BANANA_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def generate_image(
    prompt: str,
    quality: str = "flash",
    filename: str | None = None,
) -> str:
    log_write(f"[generate_image] quality:{quality} filename:{filename} prompt:{prompt[:80]}")
    print(f"{GRAY}[IMAGE GEN] Nano Banana {quality} | {prompt[:68]}{'...' if len(prompt) > 68 else ''}{RESET}")

    preferred = _NANO_BANANA_QUALITY_ALIAS.get(quality)
    model_order = _NANO_BANANA_MODELS.copy()
    if preferred and preferred in model_order:
        model_order.remove(preferred)
        model_order.insert(0, preferred)

    images_dir = os.path.join(paths.WORKSPACE_DIR, "images")
    os.makedirs(images_dir, exist_ok=True)

    if not filename or not filename.strip():
        filename = "image_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.splitext(filename.strip())[0]
    out_path = os.path.join(images_dir, filename + ".png")

    from llm_client import API_KEYS
    google_keys = API_KEYS.get("google", [])
    if not google_keys:
        return "[generate_image ERROR] No Google API key found in api.keys."

    last_error = ""
    for model_name in model_order:
        for key in google_keys:
            client = OpenAI(api_key=key, base_url=_NANO_BANANA_BASE_URL)
            try:
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                )

                content = response.choices[0].message.content or ""
                b64_match = re.search(r"data:image/\w+;base64,([A-Za-z0-9+/=]+)", content)
                if b64_match:
                    img_bytes = base64.b64decode(b64_match.group(1))
                    with open(out_path, "wb") as f:
                        f.write(img_bytes)
                    return f"Successfully generated image and saved to {out_path}"

            except Exception as e:
                last_error = str(e)

    return f"[generate_image ERROR] Failed to generate image: {last_error}"


# ── TOOL FUNCTIONS ─────────────────────────────────────────────────────────────

def run_code(bash: str, timeout: int = 0) -> str:
    """Execute shell commands in system environment after permission validation."""
    log_write(f"[run_code] timeout:{timeout} cmd:{bash}")
    print(f"{GRAY}[EXEC] {bash[:80]}{'...' if len(bash) > 80 else ''}{RESET}")

    needs_perm, reason = validate_command(bash)
    if needs_perm:
        return f"[PERMISSION DENIED] Action blocked by security policy. Reason: {reason}"

    start_t = time.time()
    try:
        if sys.platform == "win32":
            p = subprocess.Popen(
                ["cmd.exe", "/c", bash],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        else:
            p = subprocess.Popen(
                ["bash", "-c", bash],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )

        if timeout > 0:
            stdout, stderr = p.communicate(timeout=timeout)
        else:
            stdout, stderr = p.communicate()

        dur = time.time() - start_t
        log_write(f"[run_code] returncode:{p.returncode} dur:{dur:.2f}s")
        output = stdout + (f"\n[STDERR]\n{stderr}" if stderr else "")
        if p.returncode != 0:
            output += f"\n[EXIT CODE {p.returncode}]"
        return output.strip() or "[NO OUTPUT]"

    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        return f"[TIMEOUT] Command timed out after {timeout} seconds."
    except Exception as e:
        return f"[ERROR] Failed to execute command: {e}"


def _speak_blocking(text: str, debug: bool = False) -> str:
    if debug:
        print("speaking")

    safe_text = shlex.quote(render_for_voice(text))
    cmd = (
        f'edge-tts --voice "en-US-AndrewNeural" '
        f'--text {safe_text} '
        f'--write-media - | mpv -'
    )
    process = None
    try:
        process = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=(sys.platform != "win32"),
        )
        stdout, stderr = process.communicate()
        if stderr and stderr.strip():
            return stderr.strip()
        return "OK"

    except KeyboardInterrupt:
        if process is not None:
            try:
                process.kill()
            except Exception:
                pass
        return "Interrupted"

    except Exception as e:
        return f"[EXCEPTION] {e}"


def speak(text: str, block: bool = False) -> None:
    global _speak_thread
    if block:
        _speak_blocking(text)
    else:
        _speak_thread = threading.Thread(target=_speak_blocking, args=(text,), daemon=True)
        _speak_thread.start()


def sleep_mode() -> str:
    CONFIG_PATH = paths.CONFIG_FILE
    DEFAULT_CONFIG = {
        "stt_path":    os.path.join(BASE_DIR, "Termux-STT"),
        "tts_enabled": False,
        "use_groq":    False,
    }
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
    except Exception:
        config = DEFAULT_CONFIG
    STT_PATH = os.path.expanduser(config["stt_path"])

    if STT_PATH not in sys.path:
        sys.path.append(STT_PATH)

    try:
        from main import listen
        check_cmd = "where edge-tts" if sys.platform == "win32" else "which edge-tts"
        if subprocess.run(
            check_cmd,
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
    except Exception as e:
        return f"[ERR] Wake mode not initiated. Reason: {e}"
    print(f"{GRAY}[SLEEP MODE ACTIVE]{RESET}")

    while True:
        heard = listen(once=True, cleaned=False, calibrate_once=True, use_groq=config.get("use_groq", False))

        if not heard:
            continue

        low = heard.lower().strip()
        print(f"{GRAY}[HEARD] {heard}{RESET}")
        
        wake_word_heard = False
        for wake_word in WAKE_WORDS:
            if wake_word in low:
                wake_word_heard = True
        if not wake_word_heard:
            continue

        print(f"{GRAY}[WAKE WORD DETECTED]{RESET}")
        return heard


def intermediate_print(text: str, voice: bool = False) -> None:
    print("AI (Intermediate) >")
    print(render_markdown_terminal(text))
    print()
    if voice:
        speak(render_for_voice(text))


def _check_battery() -> dict:
    try:
        if sys.platform == "win32":
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-CimInstance Win32_Battery | Select-Object EstimatedChargeRemaining, BatteryStatus"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            return {"raw": res.stdout.strip()}
        else:
            try:
                from tools.wrapper_termux_battery_status import get_battery_status
                return get_battery_status()
            except Exception:
                result = subprocess.run(
                    ["termux-battery-status"],
                    capture_output=True, text=True, timeout=8
                )
                data = json.loads(result.stdout)
                return {
                    "level_pct":    data.get("percentage"),
                    "status":       data.get("status"),
                    "temperature_c": data.get("temperature"),
                    "plugged":      data.get("plugged"),
                }
    except Exception as e:
        return {"error": str(e)}


def _check_weather() -> dict:
    try:
        import urllib.request
        url = "https://wttr.in/?format=j1"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())

        forecast = []
        for day in data.get("weather", [])[:3]:
            hourly = day.get("hourly", [])
            peak = max(hourly, key=lambda h: int(h.get("FeelsLikeC", 0))) if hourly else {}

            max_uv        = max((int(h.get("uvIndex",           0)) for h in hourly), default=0)
            max_humidity  = max((int(h.get("humidity",          0)) for h in hourly), default=0)
            max_feels     = max((int(h.get("FeelsLikeC",        0)) for h in hourly), default=0)
            max_heat_idx  = max((int(h.get("HeatIndexC",        0)) for h in hourly), default=0)
            max_wind_kmph = max((int(h.get("windspeedKmph",     0)) for h in hourly), default=0)
            max_gust_kmph = max((int(h.get("WindGustKmph",      0)) for h in hourly), default=0)
            min_vis       = min((int(h.get("visibility",       99)) for h in hourly), default=99)
            total_precip  = sum((float(h.get("precipMM",       0.0)) for h in hourly))
            chance_thunder= max((int(h.get("chanceofthunder",   0)) for h in hourly), default=0)
            chance_rain   = max((int(h.get("chanceofrain",      0)) for h in hourly), default=0)
            chance_high   = max((int(h.get("chanceofhightemp",  0)) for h in hourly), default=0)

            seen = set()
            descs = []
            for h in hourly:
                d = (h.get("weatherDesc") or [{}])[0].get("value", "").strip()
                if d and d not in seen:
                    seen.add(d)
                    descs.append(d)

            forecast.append({
                "date":                    day.get("date"),
                "max_temp_c":              int(day.get("maxtempC",  0)),
                "min_temp_c":              int(day.get("mintempC",  0)),
                "max_feels_like_c":        max_feels,
                "max_heat_index_c":        max_heat_idx,
                "peak_heat_hour":          peak.get("time"),
                "max_uv_index":            max_uv,
                "max_humidity_pct":        max_humidity,
                "max_wind_kmph":           max_wind_kmph,
                "max_gust_kmph":           max_gust_kmph,
                "min_visibility_km":       min_vis,
                "total_precip_mm":         round(total_precip, 1),
                "chance_of_rain_pct":      chance_rain,
                "chance_of_thunder_pct":   chance_thunder,
                "chance_of_high_temp_pct": chance_high,
                "sun_hours":               float(day.get("sunHour", 0)),
                "uv_index_daily_max":      int(day.get("uvIndex",   0)),
                "conditions":              descs,
            })
        return {"forecast": forecast}
    except Exception as e:
        return {"error": str(e)}


def _check_storage() -> dict:
    try:
        import shutil
        total, used, free = shutil.disk_usage(os.path.expanduser("~"))
        total_gb = f"{total / (1024**3):.1f}G"
        used_gb = f"{used / (1024**3):.1f}G"
        available_gb = f"{free / (1024**3):.1f}G"
        use_percent = f"{used / total * 100:.1f}%"
        return {
            "total":       total_gb,
            "used":        used_gb,
            "available":   available_gb,
            "use_percent": use_percent,
        }
    except Exception as e:
        return {"error": str(e)}


def _check_memory() -> dict:
    try:
        if sys.platform == "win32":
            ps_code = "(Get-CimInstance Win32_OperatingSystem).TotalVisibleMemorySize, (Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_code]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            lines = result.stdout.strip().splitlines()
            if len(lines) >= 2:
                total_kb = int(lines[0].strip())
                free_kb = int(lines[1].strip())
                used_kb = total_kb - free_kb
                return {
                    "total_mb":     round(total_kb / 1024),
                    "used_mb":      round(used_kb / 1024),
                    "available_mb": round(free_kb / 1024),
                    "use_percent":  round(used_kb / total_kb * 100, 1),
                }
            return {"error": "unexpected powershell memory output"}
        else:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    key, val = line.split(":", 1)
                    info[key.strip()] = val.strip()
            total     = int(info["MemTotal"].split()[0])
            available = int(info["MemAvailable"].split()[0])
            used      = total - available
            return {
                "total_mb":     round(total     / 1024),
                "used_mb":      round(used      / 1024),
                "available_mb": round(available / 1024),
                "use_percent":  round(used / total * 100, 1),
            }
    except Exception as e:
        return {"error": str(e)}


def _check_network() -> dict:
    try:
        if sys.platform == "win32":
            cmd = ["netsh", "wlan", "show", "interfaces"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            return {"raw": res.stdout.strip()[:500]}
        else:
            try:
                from tools.wrapper_termux_wifi_scaninfo import get_wifi_scan_info
                networks = get_wifi_scan_info()
                if networks:
                    best = networks[0]
                    return {
                        "ssid":           best.get("ssid"),
                        "link_speed_mbps": "N/A",
                        "rssi_dbm":       best.get("rssi"),
                        "ip":             "N/A",
                    }
            except Exception:
                pass
            result = subprocess.run(
                ["termux-wifi-connectioninfo"],
                capture_output=True, text=True, timeout=8
            )
            data = json.loads(result.stdout)
            return {
                "ssid":           data.get("ssid"),
                "link_speed_mbps": data.get("link_speed_mbps"),
                "rssi_dbm":       data.get("rssi"),
                "ip":             data.get("ip"),
            }
    except Exception as e:
        return {"error": str(e)}


def _check_datetime() -> dict:
    now = datetime.now()
    return {
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday":  now.strftime("%A"),
        "hour":     now.hour,
    }


CHECKS = {
    "battery":  _check_battery,
    "weather":  _check_weather,
    "storage":  _check_storage,
    "memory":   _check_memory,
    "network":  _check_network,
    "datetime": _check_datetime,
}


def run_diagnosis() -> dict:
    results = {}
    with ThreadPoolExecutor(max_workers=len(CHECKS)) as executor:
        futures = {executor.submit(fn): name for name, fn in CHECKS.items()}
        for future in as_completed(futures, timeout=DIAGNOSIS_TIMEOUT):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as e:
                results[name] = {"error": str(e)}
    return results


# ── WHATSAPP CORE TOOLS ────────────────────────────────────────────────────────

def send_whatsapp_message(to_phone: str, message_text: str) -> str:
    log_write(f"[send_whatsapp_message] to:{to_phone} msg:{message_text}")
    print(f"{GRAY}[WhatsApp] Sending message to {to_phone}...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    try:
        success = whatsapp_manager.send_message(to_phone, message_text)
        if success:
            out = f"Successfully sent WhatsApp message to {to_phone}."
            print(f"{GRAY}[WhatsApp] {out}{RESET}")
            wa_log_write("SENT (manual)", to_phone, to_phone, message_text)
            return out
        else:
            out = f"Failed to send WhatsApp message to {to_phone}."
            print(f"{RED}[WhatsApp] {out}{RESET}")
            return out
    except Exception as e:
        out = f"[ERROR] Failed to send WhatsApp message: {e}"
        print(f"{RED}[WhatsApp] {out}{RESET}")
        return out


def get_whatsapp_status() -> str:
    log_write("[get_whatsapp_status]")
    print(f"{GRAY}[WhatsApp] Checking status...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    state = whatsapp_manager.connection_state

    pending = whatsapp_manager.get_pending_messages(clear=False)
    pending_str = ""
    if pending:
        pending_str = f"\nPending Messages count: {len(pending)}\n"
        for idx, msg in enumerate(pending):
            pending_str += f"- [{idx+1}] From {msg['profileName']} ({msg['sender']}): \"{msg['text']}\"\n"
    else:
        pending_str = "\nNo pending messages in queue."
        
    busy_status = "ENABLED" if whatsapp_manager.is_busy else "DISABLED"
    out = (
        f"WhatsApp Service State: {state}\n"
        f"Busy Auto-Reply Mode: {busy_status}\n"
        f"Busy Instruction: \"{whatsapp_manager.busy_instruction}\""
        f"{pending_str}"
    )
    return out


def get_whatsapp_chats(filter_type: str = "all") -> str:
    log_write(f"[get_whatsapp_chats] filter:{filter_type}")
    print(f"{GRAY}[WhatsApp] Fetching chats (filter: {filter_type})...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."

    chats = whatsapp_manager.get_chats(filter_type=filter_type)
    if not chats:
        return "No chats found or WhatsApp not ready."

    dms    = [c for c in chats if c.get("type") == "dm"]
    groups = [c for c in chats if c.get("type") == "group"]
    lines  = []

    def _fmt(c):
        parts = [f"  {c['name']}"]
        if c.get("isPinned"):  parts.append("📌")
        if c.get("isMuted"):   parts.append("🔇")
        if c.get("unread"):    parts.append(f"[{c['unread']} unread]")
        return " ".join(parts) + f"\n    JID: {c['jid']}"

    if dms:
        lines.append(f"── DMs ({len(dms)}) ──")
        lines.extend(_fmt(c) for c in dms)

    if groups:
        if lines:
            lines.append("")
        lines.append(f"── Groups ({len(groups)}) ──")
        lines.extend(_fmt(c) for c in groups)

    lines.append(f"\nTotal: {len(chats)} chat(s).")
    return "\n".join(lines)


def silence_whatsapp_contact(jid: str, hours: float = 24) -> str:
    log_write(f"[silence_whatsapp_contact] jid:{jid} hours:{hours}")
    print(f"{GRAY}[WhatsApp] Silencing {jid} for {hours} hour(s)...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    ok, msg = whatsapp_manager.silence_contact(jid, hours=hours)
    return msg


def react_to_whatsapp_message(message_id: str, emoji: str) -> str:
    log_write(f"[react_to_whatsapp_message] id:{message_id} emoji:{emoji}")
    print(f"{GRAY}[WhatsApp] Reacting to message {message_id} with {emoji}...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    ok = whatsapp_manager.react(message_id, emoji)
    return f"Reacted with {emoji}." if ok else "Failed to react — message may no longer be available."


def get_whatsapp_contact_info(jid: str) -> str:
    log_write(f"[get_whatsapp_contact_info] jid:{jid}")
    print(f"{GRAY}[WhatsApp] Fetching contact info for {jid}...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    info = whatsapp_manager.get_contact_info(jid)
    if not info:
        return f"Could not fetch contact info for {jid}."
    lines = [
        f"Name:        {info.get('name') or 'Unknown'}",
        f"Number:      {info.get('number', 'N/A')}",
        f"JID:         {info.get('jid', jid)}",
        f"In contacts: {'Yes' if info.get('isMyContact') else 'No'}",
        f"Business:    {'Yes' if info.get('isBusiness') else 'No'}",
        f"Blocked:     {'Yes' if info.get('isBlocked') else 'No'}",
    ]
    if info.get("about"):
        lines.append(f"About:       {info['about']}")
    if info.get("profilePicUrl"):
        lines.append(f"Profile pic: {info['profilePicUrl']}")
    return "\n".join(lines)


def get_whatsapp_group_participants(jid: str) -> str:
    log_write(f"[get_whatsapp_group_participants] jid:{jid}")
    print(f"{GRAY}[WhatsApp] Fetching group participants for {jid}...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    participants, group_name = whatsapp_manager.get_group_participants(jid)
    if not participants:
        return f"No participants found for {jid} (may not be a group or not ready)."
    lines = [f"Group: {group_name or jid} ({len(participants)} members)", ""]
    admins  = [p for p in participants if p.get("isAdmin") or p.get("isSuperAdmin")]
    members = [p for p in participants if not p.get("isAdmin") and not p.get("isSuperAdmin")]
    if admins:
        lines.append("Admins:")
        for p in admins:
            tag = " [owner]" if p.get("isSuperAdmin") else ""
            lines.append(f"  {p.get('number', p['jid'])}{tag}  ({p['jid']})")
    if members:
        lines.append("Members:")
        for p in members:
            lines.append(f"  {p.get('number', p['jid'])}  ({p['jid']})")
    return "\n".join(lines)


def download_whatsapp_media(message_id: str) -> str:
    log_write(f"[download_whatsapp_media] id:{message_id}")
    print(f"{GRAY}[WhatsApp] Downloading media for message {message_id}...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    result = whatsapp_manager.download_media(message_id)
    if not result:
        return "Media download failed — message may be expired or have no media."
    import base64, mimetypes, os
    mimetype = result.get("mimetype", "application/octet-stream")
    filename = result.get("filename") or f"wa_media_{message_id[:8]}"
    if "." not in filename:
        ext = mimetypes.guess_extension(mimetype) or ".bin"
        filename += ext
    out_dir = os.path.join(paths.WORKSPACE_DIR, "downloads")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, filename)
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(result["data"]))
    size_kb = os.path.getsize(out_path) // 1024
    return f"Saved to {out_path} ({size_kb} KB, {mimetype})"


def schedule_whatsapp_message(to: str, message: str, send_at: str) -> str:
    log_write(f"[schedule_whatsapp_message] to:{to} send_at:{send_at}")
    print(f"{GRAY}[WhatsApp] Scheduling message to {to} at {send_at}...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    ok, info = whatsapp_manager.schedule_message(to, message, send_at)
    return info if ok else f"Failed to schedule: {info}"


def search_whatsapp_chat(jid: str, query: str, limit: int = 20) -> str:
    log_write(f"[search_whatsapp_chat] jid:{jid} query:{query}")
    print(f"{GRAY}[WhatsApp] Searching chat {jid} for '{query}'...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    results = whatsapp_manager.search_chat(jid, query, limit=limit)
    if not results:
        return f"No messages found containing '{query}'."
    lines = [f"Found {len(results)} message(s) matching '{query}':", ""]
    for r in results:
        direction = "→ OUT" if r.get("direction") == "OUTBOUND" else "← IN"
        lines.append(f"[{r.get('timestamp', '')[:16]}] {direction}: {r.get('body', '')}")
        lines.append(f"  ID: {r.get('messageId', '')}")
    return "\n".join(lines)


def archive_whatsapp_chat(jid: str, archive: bool = True) -> str:
    log_write(f"[archive_whatsapp_chat] jid:{jid} archive:{archive}")
    print(f"{GRAY}[WhatsApp] {'Archiving' if archive else 'Unarchiving'} chat {jid}...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    ok = whatsapp_manager.archive_chat(jid, archive=archive)
    action = "Archived" if archive else "Unarchived"
    return f"{action} {jid}." if ok else f"Failed to {'archive' if archive else 'unarchive'} {jid}."


def set_whatsapp_seen(jid: str) -> str:
    log_write(f"[set_whatsapp_seen] jid:{jid}")
    print(f"{GRAY}[WhatsApp] Marking chat {jid} as read...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    whatsapp_manager.set_seen(jid)
    return f"Marked {jid} as read."


def get_pending_whatsapp_messages(clear: bool = True) -> str:
    log_write(f"[get_pending_whatsapp_messages] clear:{clear}")
    print(f"{GRAY}[WhatsApp] Retrieving pending messages (clear={clear})...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    pending = whatsapp_manager.get_pending_messages(clear=clear)
    if not pending:
        return "No pending WhatsApp messages."
    print(f"{GRAY}[WhatsApp] {len(pending)} pending message(s) retrieved.{RESET}")
    
    out_lines = []
    for msg in pending:
        group_tag = f" [GROUP: {msg.get('chatName', msg.get('sender'))}]" if msg.get('isGroup') else ""
        out_lines.append(
            f"From: {msg['profileName']} ({msg['sender']}){group_tag}\n"
            f"Time: {msg['timestamp']}\n"
            f"Message: {msg['text']}\n"
            f"History context available: {len(msg.get('context_history', []))} messages\n"
            "---"
        )
    return "\n".join(out_lines)


def fetch_whatsapp_chat_history(to_phone: str, limit: int = 5) -> str:
    log_write(f"[fetch_whatsapp_chat_history] to:{to_phone} limit:{limit}")
    print(f"{GRAY}[WhatsApp] Fetching chat history for {to_phone}...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    try:
        history = whatsapp_manager.fetch_context(to_phone, limit=limit)
        if not history:
            return f"No chat history found or could not fetch history for {to_phone}."
        
        out_lines = []
        for msg in history:
            direction = msg.get("direction", "UNKNOWN")
            body = msg.get("body", "")
            ts = msg.get("timestamp", "")
            out_lines.append(f"[{ts}] {direction}: {body}")
        return "\n".join(out_lines)
    except Exception as e:
        return f"[ERROR] Failed to fetch chat history: {e}"


def set_whatsapp_busy_mode(enabled: bool, instruction: str = "", exclude_all_groups_except: list = None) -> str:
    log_write(f"[set_whatsapp_busy_mode] enabled:{enabled} instruction:{instruction} exclude_all_groups_except:{exclude_all_groups_except}")
    print(f"{GRAY}[WhatsApp] Setting busy mode (enabled={enabled})...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    
    status_str = "ENABLED" if enabled else "DISABLED"
    print(f"{GRAY}[WhatsApp] Busy mode → {status_str}{RESET}")
    
    whatsapp_manager.set_busy(enabled, instruction)
    
    if enabled and exclude_all_groups_except is not None:
        whatsapp_manager.set_exclude_all_groups_except(exclude_all_groups_except)
        print(f"{GRAY}[WhatsApp] Group Whitelist: {exclude_all_groups_except}{RESET}")
    elif not enabled:
        whatsapp_manager.set_exclude_all_groups_except([])

    active_instruction = instruction or whatsapp_manager.busy_instruction
    if enabled:
        print(f"{GRAY}[WhatsApp] Instruction: \"{active_instruction}\"{RESET}")
    
    msg = f"WhatsApp Busy Mode set to {status_str}."
    if enabled:
        msg += f" Instruction: \"{active_instruction}\"."
        if exclude_all_groups_except:
            msg += f" Groups included: {exclude_all_groups_except} (others excluded)."
    return msg


def set_whatsapp_user_profile(profile: str) -> str:
    log_write(f"[set_whatsapp_user_profile] {profile}")
    print(f"{GRAY}[WhatsApp] Updating user profile context...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    whatsapp_manager.set_user_profile(profile)
    print(f"{GRAY}[WhatsApp] User profile updated.{RESET}")
    return f'User profile set: "{profile}"'


def get_whatsapp_report(clear: bool = False) -> str:
    log_write(f"[get_whatsapp_report] clear:{clear}")
    print(f"{GRAY}[WhatsApp] Generating conversation report (clear={clear})...{RESET}")
    
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    
    try:
        wa_log_file = os.path.join(paths.LOGS_DIR, "whatsapp_log.jsonl")
        if not os.path.exists(wa_log_file):
            return "No WhatsApp log file found. No conversations have been recorded yet."

        with open(wa_log_file, "r", encoding="utf-8") as fh:
            lines = [l.strip() for l in fh if l.strip()]

        if not lines:
            return "WhatsApp log is empty. No conversations recorded yet."

        entries = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        if not entries:
            return "WhatsApp log contains no valid entries."

        by_contact = defaultdict(list)
        for e in entries:
            by_contact[e["sender_id"]].append(e)

        report_lines = [f" WhatsApp Report — {len(entries)} total message(s) across {len(by_contact)} contact(s)\n"]
        report_lines.append("=" * 50)

        for contact_id, msgs in by_contact.items():
            contact_name = msgs[0]["sender_name"]
            report_lines.append(f"\n {contact_name} ({contact_id})")
            report_lines.append(f"   {len(msgs)} message(s):")
            for m in msgs:
                ts = m["timestamp"][:16].replace("T", " ")
                direction = m["direction"]
                text = m["message"]
                arrow = "←" if direction == "RECEIVED" else "→"
                report_lines.append(f"   [{ts}] {arrow} [{direction}] {text}")

        report_lines.append("\n" + "=" * 50)

        if clear:
            open(wa_log_file, "w", encoding="utf-8").close()
            report_lines.append(" Log cleared.")

        return "\n".join(report_lines)

    except Exception as e:
        return f"[ERROR] Failed to read WhatsApp report: {e}"


def ask_ai_simple(prompt: str, model_name: str = "gemini-2.5-flash-lite", system_prompt: str = "") -> str:
    """Execute a lightweight tool-free LLM completion for internal tasks (summaries, auto-replies)."""
    try:
        from llm_client import API_KEYS, PROVIDERS
        from openai import OpenAI

        sys_content = system_prompt or "You are a helpful assistant."
        messages = [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": prompt}
        ]

        def _infer_provider(m: str) -> tuple[str, str]:
            if m.startswith("openrouter") or "/" in m:
                return "openrouter", "https://openrouter.ai/api/v1"
            if m.startswith("gemini") or m.startswith("gemma"):
                return "google", "https://generativelanguage.googleapis.com/v1beta/openai/"
            if "llama" in m or "mixtral" in m or "qwen" in m or "gpt" in m:
                return "groq", "https://api.groq.com/openai/v1/"
            if "nvidia" in m or "nemotron" in m or "deepseek" in m:
                return "nvidia", "https://integrate.api.nvidia.com/v1"
            return "google", "https://generativelanguage.googleapis.com/v1beta/openai/"

        pid, url = _infer_provider(model_name)
        keys = API_KEYS.get(pid, [])

        if not keys:
            for p, k_list in API_KEYS.items():
                if k_list:
                    pid = p
                    url = PROVIDERS[p]["base_url"]
                    keys = k_list
                    break

        if not keys:
            return ""

        headers = {"HTTP-Referer": "https://github.com/opsonusdh/Termux-AI", "X-Title": "Termux-AI"} if pid == "openrouter" else None

        for key in keys:
            try:
                client = OpenAI(api_key=key, base_url=url, default_headers=headers)
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    max_tokens=1024
                )
                return resp.choices[0].message.content or ""
            except Exception:
                continue

        return ""
    except Exception as e:
        return ""

