import os
import re
import sys
import json
import html
import time
import shlex
import shutil
import signal
import fnmatch
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


def _safe_bool(val) -> bool:
    """Safely convert LLM-provided values to bool (handles string 'false')."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() not in ('false', '0', 'no', 'none', '')
    return bool(val)


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
                "positions as line numbers (1-indexed, inclusive) or byte offsets. "
                "NOTE: Line numbers are 1-indexed (the first line of the file is line 1, not 0)."
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
                "'segment' replaces only the specified line range or byte range. "
                "NOTE: Line numbers are 1-indexed (the first line of the file is line 1, not 0)."
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
        "type": "function",
        "function": {
            "name": "find_replace",
            "description": (
                "Find and replace all occurrences of a string in a file. "
                "Supports both single-line and multi-line find/replace patterns. "
                "Optionally restrict replacements to a specific line range (1-indexed). "
                "Returns the number of replacements made."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or ~ path to the file."
                    },
                    "find": {
                        "type": "string",
                        "description": "Text to find (can be multi-line)."
                    },
                    "replace": {
                        "type": "string",
                        "description": "Text to replace with (can be multi-line)."
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Whether the search is case-sensitive. Default: true.",
                        "default": True
                    },
                    "segment_start": {
                        "type": "integer",
                        "description": "Start line number (1-indexed) for the segment to search within. Omit to search from the beginning.",
                        "minimum": 1
                    },
                    "segment_end": {
                        "type": "integer",
                        "description": "End line number (1-indexed, inclusive) for the segment to search within. Omit to search to the end of file.",
                        "minimum": 1
                    }
                },
                "required": ["path", "find", "replace"]
            }
        }
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
                        "description": "Sub-AI model to delegate the subtask to (e.g. 'cohere/north-mini-code:free', 'google/gemma-4-26b-a4b-it:free', 'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free', 'openai/gpt-oss-120b'). Never use 'openrouter/free'.",
                        "default": "cohere/north-mini-code:free",
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
    {
        "type": "function",
        "function": {
            "name": "search_in_files",
            "description": (
                "Search file contents recursively for text. "
                "Supports filename glob filtering, case sensitivity, "
                "bounded result count, and bounded per-file size."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory to search.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Text to find in file contents.",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Filename glob such as '*.py'. Default '*'.",
                        "default": "*",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Whether matching is case-sensitive.",
                        "default": False,
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum matching lines to return.",
                        "default": 50,
                        "minimum": 1,
                        "maximum": 200,
                    },
                    "max_file_size_kb": {
                        "type": "integer",
                        "description": "Maximum file size to scan in KiB.",
                        "default": 2048,
                        "minimum": 1,
                        "maximum": 16384,
                    },
                },
                "required": ["path", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": (
                "List files and directories at a given path. "
                "Supports recursive listing with configurable depth."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or ~ path to the directory.",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "If true, list contents recursively. Default false.",
                        "default": False,
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum depth for recursive listing. Default 3.",
                        "default": 3,
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search for files and directories matching a name pattern within a directory tree. "
                "Uses glob-style matching and substring search. "
                "Automatically skips .git, node_modules, __pycache__, and virtual environments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Root directory to search in.",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern or substring to match filenames (e.g. '*.py', 'config', '*.json').",
                    },
                    "file_type": {
                        "type": "string",
                        "enum": ["all", "file", "dir"],
                        "description": "Filter by type: 'all', 'file', or 'dir'. Default 'all'.",
                        "default": "all",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return. Default 50.",
                        "default": 50,
                    },
                },
                "required": ["path", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_file",
            "description": (
                "Rename or move a file or directory to a new path. "
                "Creates parent directories if needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "old_path": {
                        "type": "string",
                        "description": "Current path of the file or directory.",
                    },
                    "new_path": {
                        "type": "string",
                        "description": "Destination path.",
                    },
                },
                "required": ["old_path", "new_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": (
                "Delete a file or directory. "
                "By default only deletes files and empty directories. "
                "Set force=true to recursively delete non-empty directories."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file or directory to delete.",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "If true, recursively delete non-empty directories. Default false.",
                        "default": False,
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": (
                "Make an HTTP/HTTPS request and return the response. "
                "Supports GET, POST, PUT, PATCH, DELETE methods. "
                "Use for API calls, webhooks, or fetching raw data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to request.",
                    },
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                        "description": "HTTP method. Default GET.",
                        "default": "GET",
                    },
                    "headers": {
                        "type": "string",
                        "description": "Headers as JSON string or comma-separated 'Key: Value' pairs.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Request body (typically JSON string for POST/PUT).",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Request timeout in seconds. Default 15.",
                        "default": 15,
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_datetime",
            "description": (
                "Get the current date, time, day of week, UTC time, "
                "and timezone offset. Use when you need to know the current time."
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
            "name": "ask_user",
            "description": (
                "Ask the user a question with optional multiple-choice options. "
                "Returns the user\'s answer as a string. If options are provided, "
                "the user can select by number (1, 2, 3...) or type a custom answer. "
                "Use this when you need clarification, confirmation, or a decision from the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask the user.",
                    },
                    "options": {
                        "type": "array",
                        "description": "Optional list of answer options. User can select by number.",
                        "items": {
                            "type": "string"
                        },
                    },
                    "default": {
                        "type": ["integer", "string"],
                        "description": "Optional default value (index starting from 1, or string value).",
                    },
                    "allow_custom": {
                        "type": "boolean",
                        "description": "If true (default), user can type a custom answer instead of selecting an option.",
                        "default": True,
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm",
            "description": (
                "Ask for yes/no confirmation from the user. "
                "Returns true for yes, false for no."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The confirmation question.",
                    },
                    "default": {
                        "type": "boolean",
                        "description": "Default value if user just presses Enter.",
                        "default": True,
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_todos",
            "description": (
                "Manage a persistent todo/task checklist saved in logs/todos.json. "
                "Break large tasks into smaller subtasks, track progress by checking/unchecking, "
                "list all tasks at once, delete or clear tasks. Data persists across sessions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "add", "list", "check", "uncheck", "delete", "clear"],
                        "description": (
                            "Action to perform: "
                            "'create' = start a new list (replaces existing), optionally with tasks; "
                            "'add' = append task(s) to existing list; "
                            "'list' = show all tasks with status; "
                            "'check' = mark a task done by ID; "
                            "'uncheck' = mark a task not-done by ID; "
                            "'delete' = remove a task by ID; "
                            "'clear' = wipe all tasks."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": "Title for the todo list (used with 'create', optionally 'add').",
                    },
                    "tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of task descriptions (used with 'create' and 'add').",
                    },
                    "task_id": {
                        "type": "integer",
                        "description": "ID of the task to check, uncheck, or delete.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Single task text (alternative to 'tasks' for 'add' action).",
                    },
                },
                "required": ["action"],
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
                lines = f.readlines()
                # Add line numbers to each line
                numbered_lines = []
                for i, line in enumerate(lines, 1):
                    numbered_lines.append(f"{i}| {line.rstrip()}")
                return "\n".join(numbered_lines)

            if unit == "lines":
                lines = f.readlines()
                start = (segment_start - 1) if segment_start and segment_start > 0 else 0
                end = segment_end if segment_end and segment_end <= len(lines) else len(lines)
                # Add line numbers to the selected segment
                numbered_lines = []
                for i, line in enumerate(lines[start:end], start + 1):
                    numbered_lines.append(f"{i}| {line.rstrip()}")
                return "\n".join(numbered_lines)
            else: # bytes
                f.seek(segment_start or 0)
                length = (segment_end - segment_start + 1) if (segment_start and segment_end) else None
                return f.read(length) if length else f.read()

    except Exception as e:
        return f"[ERROR] Failed to read file: {e}"



def write_file(
    path: str,
    content: str,
    mode: str = "overwrite",
    segment_start: int | None = None,
    segment_end: int | None = None,
    unit: str = "lines",
) -> str:
    """
    Write content to a file without using the shell.
    Supports four modes:
    'overwrite' replaces the entire file (default).
    'append' adds content to the end.
    'prepend' inserts content at the start.
    'segment' replaces only the specified line range or byte range.
    NOTE: Line numbers are 1-indexed (the first line of the file is line 1, not 0).
    """
    # Import permissions here to avoid circular imports
    from core import permissions

    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(paths.ROOT, p)

    # Protected directory check - prevent silent modification of core/ and Termux-STT/
    if permissions._is_protected(p):
        rel = os.path.relpath(os.path.normpath(os.path.realpath(p)), permissions.AI_ROOT)
        return f"[ERROR] Permission denied: '{rel}' is inside a protected directory. Use shell command with explicit permission."

    # Outside project root check
    if permissions._is_outside_root(p):
        return f"[ERROR] Permission denied: path '{path}' is outside project directory {permissions.AI_ROOT}"

    # Validate segment mode parameters
    if mode == "segment":
        if segment_start is None or segment_end is None:
            return "[ERROR] segment_start and segment_end are required for segment mode"
        if segment_start < 1:
            return "[ERROR] segment_start must be >= 1 (1-indexed)"
        if segment_end < segment_start:
            return "[ERROR] segment_end must be >= segment_start"

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
            with open(p, "w", encoding="utf-8", errors="replace") as f:
                f.write(content + existing)
            return f"Successfully prepended to {path}."

        elif mode == "segment":
            if not os.path.exists(p):
                lines = []
            else:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()

            start = segment_start - 1  # Convert to 0-indexed (validated >= 1 above)
            original_len = len(lines)
            end = min(segment_end, original_len)  # Cap at file length

            # Validate range
            if start > end:
                return f"[ERROR] segment_start ({segment_start}) exceeds file length ({original_len})"

            new_lines = content.splitlines(keepends=True)

            # Determine what kind of operation this is
            is_insert_at_end = (start == original_len) and (end == original_len)
            is_replace_through_end = (start < original_len) and (end >= original_len)
            is_middle_replace = (end < original_len)

            if is_replace_through_end and original_len > 0:
                # Replacing from middle through end (includes last line)
                if not lines[-1].endswith("\n"):
                    # Original last line had no trailing newline
                    if new_lines and new_lines[-1].endswith("\n"):
                        new_lines[-1] = new_lines[-1].rstrip("\n")
                elif new_lines and not new_lines[-1].endswith("\n"):
                    # Original had trailing newline, ensure new content does too
                    new_lines[-1] += "\n"
            elif is_insert_at_end:
                # Inserting after last line
                if original_len > 0 and not lines[-1].endswith("\n"):
                    # File does not end with newline - add one to last line first
                    lines[-1] += "\n"
                # New line should have newline
                if new_lines and not new_lines[-1].endswith("\n"):
                    new_lines[-1] += "\n"
            elif is_middle_replace:
                # Replacing middle lines only - ensure all new lines have newlines
                for i in range(len(new_lines)):
                    if not new_lines[i].endswith("\n"):
                        new_lines[i] += "\n"

            lines[start:end] = new_lines
            with open(p, "w", encoding="utf-8") as f:
                f.writelines(lines)
            # Report the intended range (segment_start..segment_end)
            return f"Successfully updated lines {segment_start}..{segment_end} in {path}."

        else:
            return f"[ERROR] Invalid write mode: {mode}"

    except Exception as e:
        return f"[ERROR] Failed to write file: {e}"



def find_replace(
    path: str,
    find: str,
    replace: str,
    case_sensitive: bool = True,
    segment_start: int | None = None,
    segment_end: int | None = None,
) -> str:
    """
    Find and replace all occurrences of a string in a file.
    Supports both single-line and multi-line find/replace patterns.
    Optionally restrict replacements to a specific line range (1-indexed).
    Returns the number of replacements made.
    """
    from core import permissions

    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(paths.ROOT, p)

    # Protected directory check
    if permissions._is_protected(p):
        rel = os.path.relpath(os.path.normpath(os.path.realpath(p)), permissions.AI_ROOT)
        return f"[ERROR] Permission denied: '{rel}' is inside a protected directory. Use shell command with explicit permission."

    # Outside project root check
    if permissions._is_outside_root(p):
        return f"[ERROR] Permission denied: path '{path}' is outside project directory {permissions.AI_ROOT}"

    if not os.path.exists(p):
        return f"[ERROR] File not found: {path}"

    if not find:
        return "[ERROR] 'find' parameter cannot be empty."

    # Validate segment parameters
    if segment_start is not None and segment_start < 1:
        return "[ERROR] segment_start must be >= 1 (1-indexed)"
    if segment_end is not None and segment_end < 1:
        return "[ERROR] segment_end must be >= 1 (1-indexed)"
    if segment_start is not None and segment_end is not None and segment_end < segment_start:
        return "[ERROR] segment_end must be >= segment_start"

    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        # If no segment specified, operate on entire file
        if segment_start is None and segment_end is None:
            content = "".join(lines)
            if case_sensitive:
                count = content.count(find)
                if count == 0:
                    return f"No occurrences of the search text found in {path}."
                new_content = content.replace(find, replace)
            else:
                import re
                pattern = re.compile(re.escape(find), re.IGNORECASE)
                matches = pattern.findall(content)
                count = len(matches)
                if count == 0:
                    return f"No occurrences of the search text found in {path}."
                new_content = pattern.sub(replace, content)

            with open(p, "w", encoding="utf-8") as f:
                f.write(new_content)

            return f"Successfully replaced {count} occurrence{'s' if count != 1 else ''} in {path}."

        # Segment specified: operate only on the specified line range
        total_lines = len(lines)
        start_idx = (segment_start - 1) if segment_start else 0
        end_idx = segment_end if segment_end else total_lines
        
        # Cap at file length
        start_idx = min(max(0, start_idx), total_lines)
        end_idx = min(max(0, end_idx), total_lines)
        
        if start_idx >= end_idx:
            return f"[ERROR] Invalid segment range: lines {segment_start}..{segment_end} (file has {total_lines} lines)"

        # Extract the segment content
        segment_lines = lines[start_idx:end_idx]
        segment_content = "".join(segment_lines)

        # Perform find/replace on segment only
        if case_sensitive:
            count = segment_content.count(find)
            if count == 0:
                return f"No occurrences of the search text found in lines {segment_start or 1}..{segment_end or total_lines} of {path}."
            new_segment_content = segment_content.replace(find, replace)
        else:
            import re
            pattern = re.compile(re.escape(find), re.IGNORECASE)
            matches = pattern.findall(segment_content)
            count = len(matches)
            if count == 0:
                return f"No occurrences of the search text found in lines {segment_start or 1}..{segment_end or total_lines} of {path}."
            new_segment_content = pattern.sub(replace, segment_content)

        # Reconstruct the file with the modified segment
        new_segment_lines = new_segment_content.splitlines(keepends=True)
        
        # Handle trailing newline consistency
        if segment_content.endswith("\n") and new_segment_lines and not new_segment_lines[-1].endswith("\n"):
            new_segment_lines[-1] += "\n"
        elif not segment_content.endswith("\n") and new_segment_lines and new_segment_lines[-1].endswith("\n"):
            new_segment_lines[-1] = new_segment_lines[-1].rstrip("\n")

        lines[start_idx:end_idx] = new_segment_lines

        with open(p, "w", encoding="utf-8") as f:
            f.writelines(lines)

        seg_desc = f"lines {segment_start or 1}..{segment_end or total_lines}"
        return f"Successfully replaced {count} occurrence{'s' if count != 1 else ''} in {seg_desc} of {path}."

    except Exception as e:
        return f"[ERROR] Failed to find and replace: {e}"



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
    {"provider_id": "openrouter", "name": "cohere/north-mini-code:free",                  "base_url": "https://openrouter.ai/api/v1"},
    {"provider_id": "openrouter", "name": "google/gemma-4-26b-a4b-it:free",               "base_url": "https://openrouter.ai/api/v1"},
    {"provider_id": "openrouter", "name": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free", "base_url": "https://openrouter.ai/api/v1"},
    {"provider_id": "openrouter", "name": "poolside/laguna-xs-2.1:free",                  "base_url": "https://openrouter.ai/api/v1"},
    {"provider_id": "openrouter", "name": "minimax/minimax-m2.7:free",                    "base_url": "https://openrouter.ai/api/v1"},
    {"provider_id": "openrouter", "name": "thinkingmachines/inkling-small:free",          "base_url": "https://openrouter.ai/api/v1"},
    {"provider_id": "openrouter", "name": "inclusionai/ling-3.0-flash-fin:free",          "base_url": "https://openrouter.ai/api/v1"},
    {"provider_id": "openrouter", "name": "liquid/lfm-2.5-2.6b:free",                     "base_url": "https://openrouter.ai/api/v1"},
    {"provider_id": "groq",       "name": "openai/gpt-oss-120b",                         "base_url": "https://api.groq.com/openai/v1/"},
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
        "search_in_files": search_in_files,
        "list_directory": list_directory,
        "search_files": search_files,
        "rename_file": rename_file,
        "delete_file": delete_file,
        "http_request": http_request,
        "get_datetime": get_datetime,
        "ask_user": ask_user,
        "confirm": confirm,
        "manage_todos": manage_todos,
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
    model: str = "cohere/north-mini-code:free",
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
        if (
            m.startswith("openrouter")
            or m.startswith("cohere/")
            or m.startswith("google/")
            or m.startswith("nvidia/")
            or m.startswith("poolside/")
            or m.startswith("thinkingmachines/")
            or m.startswith("z-ai/")
            or m.startswith("minimax/")
            or m.startswith("inclusionai/")
            or m.startswith("liquid/")
            or m.startswith("deepseek/")
            or m.startswith("qwen/")
            or "/" in m
        ):
            return "openrouter", "https://openrouter.ai/api/v1"
        if m.startswith("gemini") or m.startswith("gemma"):
            return "google", "https://generativelanguage.googleapis.com/v1beta/openai/"
        if "llama" in m or "mixtral" in m or "qwen" in m or "gpt" in m:
            return "groq", "https://api.groq.com/openai/v1/"
        if "nvidia" in m or "nemotron" in m or "deepseek" in m:
            return "nvidia", "https://integrate.api.nvidia.com/v1"
        return "google", "https://generativelanguage.googleapis.com/v1beta/openai/"

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


# ── NEW UTILITY TOOLS ──────────────────────────────────────────────────────────

def list_directory(path: str, recursive: bool = False, max_depth: int = 3) -> str:
    """List files and directories at a given path."""
    log_write(f"[list_directory] path:{path} recursive:{recursive} max_depth:{max_depth}")
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(paths.ROOT, p)

    if not os.path.isdir(p):
        return f"[ERROR] Directory not found: {path}"

    try:
        entries = []
        if not recursive:
            for name in sorted(os.listdir(p)):
                full = os.path.join(p, name)
                if os.path.isdir(full):
                    entries.append(f"  [DIR]  {name}/")
                else:
                    try:
                        size = os.path.getsize(full)
                        entries.append(f"  [FILE] {name}  ({size} bytes)")
                    except OSError:
                        entries.append(f"  [FILE] {name}")
                if len(entries) >= 200:
                    entries.append(f"  ... (truncated at 200 entries)")
                    break
        else:
            count = 0
            for root, dirs, files in os.walk(p):
                depth = root.replace(p, "").count(os.sep)
                if depth >= max_depth:
                    dirs.clear()
                    continue
                indent = "  " * depth
                rel = os.path.relpath(root, p)
                if rel == ".":
                    entries.append(f"{os.path.basename(p)}/")
                else:
                    entries.append(f"{indent}[DIR]  {os.path.basename(root)}/")
                sub_indent = "  " * (depth + 1)
                for f in sorted(files):
                    entries.append(f"{sub_indent}[FILE] {f}")
                    count += 1
                    if count >= 200:
                        entries.append(f"{sub_indent}... (truncated at 200 entries)")
                        break
                if count >= 200:
                    break

        header = f"Directory listing: {p}\n{'=' * 40}"
        return header + "\n" + "\n".join(entries) if entries else header + "\n  (empty directory)"
    except Exception as e:
        return f"[ERROR] Failed to list directory: {e}"


def search_files(path: str, pattern: str, file_type: str = "all", max_results: int = 50) -> str:
    """Search for files and directories matching a name pattern."""
    log_write(f"[search_files] path:{path} pattern:{pattern} type:{file_type}")
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(paths.ROOT, p)

    if not os.path.isdir(p):
        return f"[ERROR] Directory not found: {path}"

    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache"}
    results = []

    try:
        for root, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs if d not in skip_dirs]

            candidates = []
            if file_type in ("all", "dir"):
                candidates.extend((d, True) for d in dirs)
            if file_type in ("all", "file"):
                candidates.extend((f, False) for f in files)

            for name, is_dir in candidates:
                if fnmatch.fnmatch(name, pattern) or pattern.lower() in name.lower():
                    rel = os.path.relpath(os.path.join(root, name), p)
                    tag = "[DIR] " if is_dir else "[FILE]"
                    results.append(f"  {tag} {rel}")
                    if len(results) >= max_results:
                        break
            if len(results) >= max_results:
                break

        if not results:
            return f"No matches found for '{pattern}' in {path}."
        header = f"Search results for '{pattern}' in {p} ({len(results)} match{'es' if len(results) != 1 else ''}):\n"
        if len(results) >= max_results:
            header += f"(truncated at {max_results} results)\n"
        return header + "\n".join(results)
    except Exception as e:
        return f"[ERROR] Failed to search files: {e}"


def rename_file(old_path: str, new_path: str) -> str:
    """Rename or move a file or directory."""
    log_write(f"[rename_file] {old_path} -> {new_path}")
    op = os.path.expanduser(old_path)
    if not os.path.isabs(op):
        op = os.path.join(paths.ROOT, op)
    np = os.path.expanduser(new_path)
    if not os.path.isabs(np):
        np = os.path.join(paths.ROOT, np)

    if not os.path.exists(op):
        return f"[ERROR] Source not found: {old_path}"

    try:
        os.makedirs(os.path.dirname(np), exist_ok=True)
        shutil.move(op, np)
        return f"Successfully moved '{old_path}' to '{new_path}'."
    except Exception as e:
        return f"[ERROR] Failed to rename/move: {e}"


def delete_file(path: str, force: bool = False) -> str:
    """Delete a file or directory with safety checks."""
    log_write(f"[delete_file] path:{path} force:{force}")
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(paths.ROOT, p)

    real = os.path.realpath(p)
    if real == "/" or real == os.path.realpath(AI_ROOT):
        return f"[ERROR] Refusing to delete protected path: {real}"

    if not os.path.exists(p):
        return f"[ERROR] Path not found: {path}"

    try:
        if os.path.isfile(p) or os.path.islink(p):
            os.remove(p)
            return f"Successfully deleted file: {path}"
        elif os.path.isdir(p):
            if not os.listdir(p):
                os.rmdir(p)
                return f"Successfully deleted empty directory: {path}"
            elif force:
                shutil.rmtree(p)
                return f"Successfully deleted directory and all contents: {path}"
            else:
                return (
                    f"[ERROR] Directory '{path}' is not empty. "
                    f"Set force=true to recursively delete."
                )
        else:
            return f"[ERROR] Unknown file type at: {path}"
    except Exception as e:
        return f"[ERROR] Failed to delete: {e}"


def http_request(
    url: str,
    method: str = "GET",
    headers: str | None = None,
    body: str | None = None,
    timeout: int = 15,
) -> str:
    """Make an HTTP/HTTPS request and return the response."""
    log_write(f"[http_request] {method} {url}")
    print(f"{GRAY}[HTTP] {method} {url[:80]}{'...' if len(url) > 80 else ''}{RESET}")

    parsed_headers = {}
    if headers:
        try:
            parsed_headers = json.loads(headers)
        except (json.JSONDecodeError, TypeError):
            for line in headers.split(","):
                if ":" in line:
                    k, v = line.split(":", 1)
                    parsed_headers[k.strip()] = v.strip()

    try:
        kwargs = {
            "method": method.upper(),
            "url": url,
            "headers": parsed_headers or None,
            "timeout": min(max(1, timeout), 60),
        }

        if body and method.upper() in ("POST", "PUT", "PATCH"):
            if "Content-Type" not in parsed_headers and "content-type" not in parsed_headers:
                try:
                    json.loads(body)
                    parsed_headers["Content-Type"] = "application/json"
                    kwargs["headers"] = parsed_headers
                except (json.JSONDecodeError, TypeError):
                    pass
            kwargs["data"] = body.encode("utf-8") if isinstance(body, str) else body

        resp = requests.request(**kwargs)

        important_headers = {k: v for k, v in resp.headers.items()
                            if k.lower() in ("content-type", "content-length", "server",
                                             "date", "location", "x-ratelimit-remaining")}

        resp_body = resp.text[:10000]
        if len(resp.text) > 10000:
            resp_body += f"\n... (truncated, total {len(resp.text)} chars)"

        parts = [
            f"Status: {resp.status_code} {resp.reason}",
            f"Headers: {json.dumps(important_headers, indent=2)}",
            f"Body:\n{resp_body}",
        ]
        return "\n".join(parts)
    except requests.exceptions.Timeout:
        return f"[ERROR] Request timed out after {timeout}s."
    except requests.exceptions.ConnectionError as e:
        return f"[ERROR] Connection failed: {e}"
    except Exception as e:
        return f"[ERROR] HTTP request failed: {e}"


def get_datetime() -> str:
    """Get the current date, time, timezone, and related info."""
    log_write("[get_datetime]")
    now = datetime.now()
    try:
        from datetime import timezone as _tz
        utc_now = datetime.now(_tz.utc)
        offset = now.astimezone().strftime("%z")
        tz_name = time.tzname[time.daylight] if time.daylight else time.tzname[0]
    except Exception:
        utc_now = None
        offset = "unknown"
        tz_name = "unknown"

    lines = [
        f"Local Time : {now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Day of Week: {now.strftime('%A')}",
        f"Date       : {now.strftime('%B %d, %Y')}",
    ]
    if utc_now:
        lines.append(f"UTC Time   : {utc_now.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.extend([
        f"Timezone   : {tz_name} (UTC{offset[:3]}:{offset[3:]})",
        f"Unix Epoch : {int(now.timestamp())}",
    ])
    return "\n".join(lines)

def search_in_files(
    path: str,
    query: str,
    file_pattern: str = "*",
    case_sensitive: bool = False,
    max_results: int = 50,
    max_file_size_kb: int = 2048,
) -> str:
    """Search text content across a directory without traversing symlinked directories."""
    log_write(
        f"[search_in_files] path:{path} query:{query[:80]!r} "
        f"pattern:{file_pattern} case_sensitive:{case_sensitive}"
    )
    print(
        f"{GRAY}[SEARCH] {path} | {query[:72]}"
        f"{'...' if len(query) > 72 else ''}{RESET}"
    )

    p = os.path.expanduser(str(path or "").strip())
    if not os.path.isabs(p):
        p = os.path.join(paths.ROOT, p)

    query = str(query or "")
    file_pattern = str(file_pattern or "*")
    case_sensitive = bool(case_sensitive)
    try:
        max_results = max(1, min(200, int(max_results)))
        max_file_size_kb = max(1, min(16384, int(max_file_size_kb)))
    except (TypeError, ValueError):
        return "[ERROR] Invalid numeric search limits."

    if not query:
        return "[ERROR] Search query must not be empty."
    if not os.path.isdir(p):
        return f"[ERROR] Directory not found: {path}"

    needle = query if case_sensitive else query.casefold()
    skip_dirs = {
        ".git", "node_modules", "__pycache__", ".venv",
        "venv", ".tox", ".mypy_cache",
    }
    matches = []
    scanned = 0

    try:
        for root, dirs, files in os.walk(p, topdown=True, followlinks=False):
            dirs[:] = [
                d for d in dirs
                if d not in skip_dirs
                and not os.path.islink(os.path.join(root, d))
            ]

            for filename in sorted(files, key=str.casefold):
                full = os.path.join(root, filename)

                # Never follow symlinked files into another target.
                if os.path.islink(full):
                    continue
                if not fnmatch.fnmatchcase(filename.casefold(), file_pattern.casefold()):
                    continue

                try:
                    if os.path.getsize(full) > max_file_size_kb * 1024:
                        continue

                    with open(
                        full, "r", encoding="utf-8", errors="replace"
                    ) as fh:
                        for lineno, line in enumerate(fh, 1):
                            haystack = line if case_sensitive else line.casefold()
                            if needle in haystack:
                                rel = os.path.relpath(full, p)
                                matches.append(
                                    f"{rel}:{lineno}: {line.rstrip()}"
                                )
                                if len(matches) >= max_results:
                                    result = (
                                        f"Content search for {query!r} in {p}\n"
                                        f"{'=' * 60}\n"
                                        f"{chr(10).join(matches)}\n"
                                        f"... (truncated at {max_results} results)"
                                    )
                                    return result
                    scanned += 1
                except (OSError, UnicodeError):
                    continue

        if not matches:
            return f"No matches found for {query!r} in {p}."

        return (
            f"Content search for {query!r} in {p}\n"
            f"{'=' * 60}\n"
            f"{chr(10).join(matches)}\n"
            f"Files scanned: {scanned}"
        )
    except OSError as exc:
        return f"[ERROR] Failed to search file contents: {exc}"


# ── TOOL FUNCTIONS ─────────────────────────────────────────────────────────────

def run_code(bash: str, timeout: int = 0) -> str:
    """Execute shell commands in system environment after permission validation."""
    log_write(f"[run_code] timeout:{timeout} cmd:{bash}")
    print(f"{GRAY}[EXEC] {bash[:80]}{'...' if len(bash) > 80 else ''}{RESET}")

    allowed, reason = validate_command(bash)
    if not allowed:
        return f"[PERMISSION DENIED] Action blocked by security policy. Reason: {reason}"

    start_t = time.time()
    try:
        if sys.platform == "win32":
            p = subprocess.Popen(
                ["cmd.exe", "/c", bash],
                cwd=AI_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        else:
            p = subprocess.Popen(
                ["bash", "-c", bash],
                cwd=AI_ROOT,
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
        "show_details": False,
        "autonomous":  False,
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


def ask_ai_simple(prompt: str, model_name: str = "openai/gpt-oss-120b", system_prompt: str = "") -> str:
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

# ── USER INTERACTION TOOLS ────────────────────────────────────────────────────

def ask_user(
    question: str,
    options: list = None,
    default=None,
    allow_custom: bool = True,
    timeout: int = 0
) -> str:
    """
    Ask the user a question with optional multiple-choice options.
    
    Args:
        question: The question to ask the user.
        options: Optional list of answer options. If provided, user can select by number (1, 2, 3...).
        default: Optional default value (index starting from 1, or string value).
        allow_custom: If True (default), user can type a custom answer instead of selecting an option.
        timeout: Timeout in seconds (0 = no timeout). Not implemented for input().
    
    Returns:
        The user's answer as a string. If options provided and user selects by number,
        returns the corresponding option text. Otherwise returns raw input.
    """
    # Build the prompt
    prompt_parts = [question]
    
    if options:
        prompt_parts.append("\nOptions:")
        for i, opt in enumerate(options, 1):
            prompt_parts.append(f"  {i}) {opt}")
        
        if allow_custom:
            prompt_parts.append("\nEnter number, or type your own answer:")
        else:
            prompt_parts.append(f"\nEnter number (1-{len(options)}):")
    else:
        if default is not None:
            prompt_parts.append(f" [{default}]")
        prompt_parts.append(": ")
    
    prompt = "\n".join(prompt_parts) + " "
    
    # Get user input
    try:
        user_input = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return "[USER CANCELLED]"
    
    # Handle empty input with default
    if not user_input and default is not None:
        if isinstance(default, int) and options and 1 <= default <= len(options):
            return options[default - 1]
        return str(default)
    
    # If options provided, try to parse as number
    if options:
        try:
            choice = int(user_input)
            if 1 <= choice <= len(options):
                return options[choice - 1]
        except ValueError:
            pass  # Not a number, treat as custom input
        
        # If not a valid number and custom not allowed, ask again
        if not allow_custom:
            print(f"Please enter a number between 1 and {len(options)}.")
            return ask_user(question, options, default, allow_custom, timeout)
    
    return user_input


def confirm(question: str, default: bool = True) -> bool:
    """
    Ask for yes/no confirmation.
    
    Args:
        question: The confirmation question.
        default: Default value if user just presses Enter.
    
    Returns:
        True for yes, False for no.
    """
    suffix = " [Y/n]: " if default else " [y/N]: "
    
    try:
        response = input(question + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default
    
    if not response:
        return default
    
    return response in ('y', 'yes', 'true', '1')



# ── TODO MANAGER ───────────────────────────────────────────────────────────────

_TODO_FILE = os.path.join(paths.LOGS_DIR, "todos.json")


def _load_todos() -> dict:
    """Load the todo data from logs/todos.json."""
    if not os.path.exists(_TODO_FILE):
        return {"title": "My Tasks", "tasks": [], "next_id": 1}
    try:
        with open(_TODO_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"title": "My Tasks", "tasks": [], "next_id": 1}
        data.setdefault("title", "My Tasks")
        data.setdefault("tasks", [])
        data.setdefault("next_id", max((t.get("id", 0) for t in data["tasks"]), default=0) + 1)
        return data
    except (json.JSONDecodeError, Exception):
        return {"title": "My Tasks", "tasks": [], "next_id": 1}


def _save_todos(data: dict) -> None:
    """Save todo data to logs/todos.json."""
    os.makedirs(os.path.dirname(_TODO_FILE), exist_ok=True)
    with open(_TODO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _format_todos(data: dict) -> str:
    """Pretty-print the todo list."""
    title = data.get("title", "My Tasks")
    tasks = data.get("tasks", [])
    if not tasks:
        return f"📋 {title}\n{'─' * 40}\n  (no tasks yet)\n"
    total = len(tasks)
    done = sum(1 for t in tasks if t.get("done"))
    lines = [
        f"📋 {title}",
        f"   Progress: {done}/{total} completed ({round(done / total * 100)}%)",
        f"{'─' * 40}",
    ]
    for t in tasks:
        box = "✅" if t.get("done") else "⬜"
        lines.append(f"  [{t['id']}] {box} {t['text']}")
    return "\n".join(lines) + "\n"


def manage_todos(
    action: str,
    title: str | None = None,
    tasks: list | None = None,
    task_id: int | None = None,
    text: str | None = None,
) -> str:
    """
    Manage a persistent todo/task checklist stored in logs/todos.json.

    Actions:
      create  — start a new list, optionally with initial tasks; replaces existing list.
      add     — append one or more tasks to the existing list.
      list    — show all tasks with their status.
      check   — mark a task as done by ID.
      uncheck — mark a task as not-done by ID.
      delete  — remove a task by ID.
      clear   — wipe all tasks.
    """
    log_write(f"[manage_todos] action:{action} title:{title} task_id:{task_id}")

    if action == "clear":
        _save_todos({"title": "My Tasks", "tasks": [], "next_id": 1})
        return "🧹 All tasks cleared. The todo list is now empty."

    if action == "list":
        data = _load_todos()
        return _format_todos(data)

    if action == "create":
        new_title = title or "My Tasks"
        new_tasks = []
        task_texts = tasks if tasks else ([text] if text else [])
        next_id = 1
        for desc in task_texts:
            if desc and desc.strip():
                new_tasks.append({"id": next_id, "text": desc.strip(), "done": False})
                next_id += 1
        data = {"title": new_title, "tasks": new_tasks, "next_id": next_id}
        _save_todos(data)
        return f"✅ Created new todo list '{new_title}' with {len(new_tasks)} task(s).\n" + _format_todos(data)

    if action == "add":
        data = _load_todos()
        task_texts = tasks if tasks else ([text] if text else [])
        added = 0
        for desc in task_texts:
            if desc and desc.strip():
                tid = data["next_id"]
                data["tasks"].append({"id": tid, "text": desc.strip(), "done": False})
                data["next_id"] = tid + 1
                added += 1
        if title:
            data["title"] = title
        _save_todos(data)
        msg = f"Added {added} task(s) to '{data['title']}'."
        if added == 0:
            msg = "No task text provided. Nothing was added."
        return msg + "\n" + _format_todos(data)

    if action in ("check", "uncheck"):
        if task_id is None:
            return f"[ERROR] task_id is required for '{action}' action."
        data = _load_todos()
        target = next((t for t in data["tasks"] if t["id"] == task_id), None)
        if target is None:
            return f"[ERROR] No task found with ID {task_id}."
        if action == "check":
            target["done"] = True
            _save_todos(data)
            return f"✅ Checked task {task_id}: {target['text']}\n" + _format_todos(data)
        else:
            target["done"] = False
            _save_todos(data)
            return f"⬜ Unchecked task {task_id}: {target['text']}\n" + _format_todos(data)

    if action == "delete":
        if task_id is None:
            return "[ERROR] task_id is required for 'delete' action."
        data = _load_todos()
        target = next((t for t in data["tasks"] if t["id"] == task_id), None)
        if target is None:
            return f"[ERROR] No task found with ID {task_id}."
        data["tasks"] = [t for t in data["tasks"] if t["id"] != task_id]
        _save_todos(data)
        return f"🗑️ Deleted task {task_id}: {target['text']}\n" + _format_todos(data)

    return f"[ERROR] Unknown action '{action}'. Valid: create, add, list, check, uncheck, delete, clear."
