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
from pathlib import Path
from openai import OpenAI
from urllib.parse import urljoin
from datetime import datetime
from collections import defaultdict
from bs4 import BeautifulSoup, NavigableString, Tag
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

def _load_api_keys() -> dict[str, list[str]]:
    """Load API keys from config/api.keys.
    Supports both JSON dict format and legacy plain-text (defaults to google)."""
    path = paths.API_KEYS_FILE
    if not os.path.exists(path):
        return {}
    try:
        raw = open(path, "r", encoding="utf-8").read().strip()
        data = json.loads(raw)
        return {k: (v if isinstance(v, list) else [v]) for k, v in data.items()}
    except (json.JSONDecodeError, FileNotFoundError, AttributeError):
        # Fallback to legacy plain-text (Google only)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return {"google": [line.strip() for line in f if line.strip()]}
        except:
            return {}

API_KEYS = _load_api_keys()

BASE_DIR    = _ROOT_DIR
LOG_FILE    = os.path.join(paths.LOGS_DIR, "log.txt")
WA_LOG_FILE = os.path.join(paths.LOGS_DIR, "whatsapp_log.jsonl")

if not os.path.exists(LOG_FILE):
    open(LOG_FILE, "a", encoding="utf-8").close()
if not os.path.exists(WA_LOG_FILE):
    open(WA_LOG_FILE, "a", encoding="utf-8").close()


def log_write(message: str) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(message.rstrip("\n") + "\n")


def wa_log_write(direction: str, sender_name: str, sender_id: str, message: str) -> None:
    """Append one WhatsApp conversation entry to the persistent log file."""
    if not WP_AVAILABLE:
        return
    entry = {
        "timestamp": datetime.now().isoformat(),
        "direction": direction,
        "sender_name": sender_name,
        "sender_id": sender_id,
        "message": message,
    }
    with open(WA_LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

#  MEMORY STORE

#  Paths

MEMORY_FILE = paths.MEMORY_FILE        # personal / conversational facts
INDEX_FILE  = paths.INDEXED_MEMORY_FILE  # bulk file / code chunks

#  Stop-word filter

_STOP_WORDS = {
    "a", "an", "the", "is", "it", "in", "on", "at", "to", "do", "be",
    "of", "and", "or", "for", "with", "that", "this", "i", "you", "we",
    "me", "my", "your", "how", "what", "when", "where", "can", "could",
    "would", "should", "will", "if", "then", "so", "are", "was", "were",
    "have", "has", "had", "not", "but", "from", "use", "get", "let",
    "run", "its", "just", "want", "need", "try", "also", "any", "some",
    "all", "no", "more", "about", "by", "up", "as", "into", "out", "now",
}

#  Category tree (semantic grouping)

_CATEGORY_TREE = {
    "preference": ["shell", "ui", "style", "commands", "help", "flag", "output"],
    "instruction": ["shutdown", "process", "kill", "safety", "behavior", "close"],
    "project":     ["termux", "tui", "ai_root", "workspace", "repo", "code"],
    "fact":        ["environment", "device", "installed", "paths", "api", "key"],
    "workflow":    ["git", "python", "download", "script", "build", "install"],
}

#  Entry patterns

# Structured format: [type][tags][priority] text
_STRUCT_RE = re.compile(
    r"^\[(?P<type>\w+)\]\[(?P<tags>[^\]]*)\]\[(?P<priority>\d+)\]\s*(?P<text>.+)$"
)

# Legacy labeled format: "Learned: ...", "Instruction: ...", etc.
_LEGACY_RE = re.compile(
    r"^(?:Learned|Note|Instruction|Tip|Fact|Preference):\s*(.+)$",
    re.IGNORECASE,
)

# Tag written by index_memory(); lets retrieve() separate the two stores
_INDEXED_TAG = "indexed"


#  MemoryEntry

class MemoryEntry:
    __slots__ = ("id", "type", "tags", "priority", "text", "keywords")

    def __init__(self, id_, type_, tags_str, priority, text):
        self.id       = id_
        self.type     = type_.lower().strip()
        self.tags     = {t.strip().lower() for t in tags_str.split(",") if t.strip()}
        self.priority = max(1, min(10, int(priority)))
        self.text     = text.strip()
        self.keywords = _tokenize(self.text) | self.tags

    @property
    def is_indexed(self) -> bool:
        """True for bulk file/code chunks written by index_memory()."""
        return _INDEXED_TAG in self.tags

    def __repr__(self):
        tag_str = ",".join(sorted(self.tags))
        return f"<Mem [{self.type}][{tag_str}][{self.priority}] {self.text[:60]}>"


#  Internal helpers

def _tokenize(text: str) -> set:
    words = re.findall(r"[a-z0-9_\-]+", text.lower())
    return {w for w in words if w not in _STOP_WORDS and len(w) > 2}


def _infer_type_tags(text: str) -> tuple:
    """Guess type and tags for plain un-tagged legacy text."""
    low = text.lower()
    if any(k in low for k in ("kill", "close", "shutdown", "goodbye", "exit", "process")):
        return "instruction", "shutdown,process"
    if any(k in low for k in ("prefer", "-h", "--help", "instead", "flag", "better")):
        return "preference", "shell,commands,help"
    if any(k in low for k in ("repo", "directory", "workspace", "folder", "project", "lives in")):
        return "project", "ai_root,workspace"
    if any(k in low for k in ("install", "package", "path", "bin", "env", "api")):
        return "fact", "environment"
    return "fact", "general"


#  Load

def load_memories(file_path: str = MEMORY_FILE, start_id: int = 0) -> list:
    """
    Parse a memory file -> list[MemoryEntry].
    Supports structured, legacy-labeled, and bare-text lines.
    Lines starting with '#' are comments and are skipped.
    start_id offsets IDs so primary and indexed entries never collide.
    """
    if not os.path.exists(file_path):
        return []

    entries = []
    with open(file_path, "r", encoding="utf-8") as fh:
        for idx, raw in enumerate(fh):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            m = _STRUCT_RE.match(line)
            if m:
                entries.append(MemoryEntry(
                    id_=start_id + idx,
                    type_=m.group("type"),
                    tags_str=m.group("tags"),
                    priority=m.group("priority"),
                    text=m.group("text"),
                ))
                continue

            m2 = _LEGACY_RE.match(line)
            if m2:
                text = m2.group(1)
                type_, tags = _infer_type_tags(text)
                entries.append(MemoryEntry(
                    id_=start_id + idx,
                    type_=type_,
                    tags_str=tags,
                    priority=7,
                    text=text,
                ))
                continue

            if len(line) > 8:
                type_, tags = _infer_type_tags(line)
                entries.append(MemoryEntry(
                    id_=start_id + idx,
                    type_=type_,
                    tags_str=tags,
                    priority=5,
                    text=line,
                ))

    return entries


#  Scoring

def _score_entry(entry: MemoryEntry, prompt_kw: set, relevant_cats: set) -> float:
    keyword_overlap   = len(entry.keywords & prompt_kw)
    tag_match_bonus   = len(entry.tags & prompt_kw) * 1.5
    priority_weight   = entry.priority * 0.4
    parent_node_boost = 2.0 if entry.type in relevant_cats else 0.0
    return keyword_overlap + tag_match_bonus + priority_weight + parent_node_boost


def _relevant_categories(prompt_kw: set) -> set:
    scores = defaultdict(float)
    for cat, subtags in _CATEGORY_TREE.items():
        if cat in prompt_kw:
            scores[cat] += 2.0
        scores[cat] += len(set(subtags) & prompt_kw) * 1.5
    top = {cat for cat, s in scores.items() if s > 0}
    return top if top else set(_CATEGORY_TREE.keys())


def make_client(key, base_url="https://generativelanguage.googleapis.com/v1beta/openai/"):
    return OpenAI(
        api_key=key,
        base_url=base_url
    )

def ask_ai_simple(prompt: str, _model, _sys_prompt) -> str:
    # Build rotation stack: (provider_id, model_name, base_url)
    providers_info = [
        ("google", "gemini-2.5-flash", "https://generativelanguage.googleapis.com/v1beta/openai/"),
        ("groq",   "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1/"),
        ("nvidia", "nvidia/llama-3.1-nemotron-nano-8b-v1", "https://integrate.api.nvidia.com/v1")
    ]
    
    rotation = []
    for pid, model, url in providers_info:
        keys = API_KEYS.get(pid, [])
        for k in keys:
            rotation.append({
                "key": k,
                "model": model,
                "base_url": url,
                "pid": pid
            })

    if not rotation:
        return "[ERROR: No API keys configured in api.keys]"

    ind = 0
    attempts = 0
    max_total_attempts = len(rotation) * 2

    while attempts < max_total_attempts:
        cfg = rotation[ind]
        client = make_client(cfg["key"], cfg["base_url"])
        try:
            response = client.chat.completions.create(
                model=cfg["model"],
                messages=[
                    {"role": "system", "content": _sys_prompt},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=1024,
            )
            msg = response.choices[0].message
            if msg.content:
                return msg.content.strip()
            return "[EMPTY RESPONSE]"

        except Exception as e:
            msg_str = str(e).upper()
            is_transient = any(x in msg_str for x in ["503", "UNAVAILABLE", "OVERLOADED", "429", "RESOURCE_EXHAUSTED", "RATE LIMIT"])
            
            if is_transient:
                print(f"{RED}[{cfg['pid']}] Key/Provider rate-limited or overloaded. Trying next...{RESET}")
                time.sleep(2)
            elif "API_KEY_INVALID" in msg_str:
                print(f"{RED}[{cfg['pid']}] Invalid API key detected.{RESET}")
            else:
                print(f"{RED}[{cfg['pid']}] API Error: {msg_str[:100]}...{RESET}")
            
            ind = (ind + 1) % len(rotation)
            attempts += 1
            
    return "[ERROR: All providers and keys failed after multiple attempts]"

#  Retrieve (separated budgets for primary vs indexed)
def is_wake_relevant(text: str) -> bool:

    sys_prompt = """
You are a wake-word relevance classifier.

The assistant name is Orion.

Determine whether the speaker is directly addressing the assistant.

Reply ONLY with:
YES
or
NO
"""
    result = ask_ai_simple(
        prompt=text,
        _model="gemini-2.5-flash-lite",
        _sys_prompt=sys_prompt,
    )

    return result.strip().upper().startswith("YES")


def retrieve(
    prompt: str,
    top_k: int = 5,
    threshold: float = 1.5,
    indexed_top_k: int = 2,
    indexed_threshold: float = 2.5,
) -> dict:
    """
    Best-first retrieval with separate budgets for the two stores.

    Primary store (memories.txt)
        Standard threshold (1.5). Up to top_k results.
        Priority-10 instructions always surface regardless of score.

    Indexed store (indexed_memory.txt)
        Higher threshold (2.5) -- only surface clearly relevant chunks.
        Capped at indexed_top_k (2) so bulk code never crowds out personal context.

    Returns {"primary": list[MemoryEntry], "indexed": list[MemoryEntry]}.
    """
    primary_entries = load_memories(MEMORY_FILE)
    indexed_entries = load_memories(INDEX_FILE, start_id=len(primary_entries))

    prompt_kw = _tokenize(prompt)

    # Primary store
    primary_results: list = []
    seen_ids: set         = set()

    if not primary_entries:
        pass
    elif not prompt_kw:
        instructions = [e for e in primary_entries if e.type == "instruction"]
        instructions.sort(key=lambda e: -e.priority)
        primary_results = instructions[:top_k]
        seen_ids = {e.id for e in primary_results}
    else:
        relevant_cats = _relevant_categories(prompt_kw)

        mandatory   = [e for e in primary_entries if e.priority == 10 and e.type == "instruction"]
        seen_ids    = {e.id for e in mandatory}
        primary_results.extend(mandatory)

        heap = []
        for entry in primary_entries:
            score = _score_entry(entry, prompt_kw, relevant_cats)
            if score >= threshold:
                heapq.heappush(heap, (-score, entry.id, entry))

        while heap and len(primary_results) < top_k:
            _, _, entry = heapq.heappop(heap)
            if entry.id not in seen_ids:
                seen_ids.add(entry.id)
                primary_results.append(entry)

    # Indexed store
    indexed_results: list = []

    if indexed_entries and prompt_kw and indexed_top_k > 0:
        relevant_cats = _relevant_categories(prompt_kw)
        heap = []
        for entry in indexed_entries:
            score = _score_entry(entry, prompt_kw, relevant_cats)
            if score >= indexed_threshold:
                heapq.heappush(heap, (-score, entry.id, entry))

        while heap and len(indexed_results) < indexed_top_k:
            _, _, entry = heapq.heappop(heap)
            indexed_results.append(entry)

    return {"primary": primary_results, "indexed": indexed_results}


def build_memory_block(prompt: str) -> str:
    """
    Retrieve relevant memories and format them as a two-section system-prompt block.

      ## MEMORY          -- personal facts, preferences, instructions
      ## RELEVANT CODE   -- indexed file/doc chunks (only when clearly relevant)

    Returns empty string if nothing is relevant.
    """
    result  = retrieve(prompt)
    primary = result["primary"]
    indexed = result["indexed"]

    if not primary and not indexed:
        return ""

    lines = []

    if primary:
        lines.append("## MEMORY")
        for entry in primary:
            tag_str = ",".join(sorted(entry.tags)) if entry.tags else entry.type
            lines.append(f"- [{entry.type}][{tag_str}] {entry.text}")
        lines.append("")

    if indexed:
        lines.append("## RELEVANT CODE/DOCS")
        for entry in indexed:
            source_tags = sorted(entry.tags - {_INDEXED_TAG})
            src = source_tags[0] if source_tags else "file"
            lines.append(f"- [{src}] {entry.text}")
        lines.append("")

    return "\n".join(lines)


def retrieve_flat(prompt: str, top_k: int = 5) -> list:
    """
    Single flat list for the retrieve_memory tool.
    Primary entries first; indexed fill remaining slots (capped at 3).
    """
    result = retrieve(
        prompt,
        top_k=max(top_k - 1, 1),
        indexed_top_k=min(3, top_k),
    )
    seen_ids: set = set()
    flat = []
    for entry in result["primary"] + result["indexed"]:
        if entry.id not in seen_ids:
            seen_ids.add(entry.id)
            flat.append(entry)
    return flat[:top_k]


#  Chunk helper (used by index_memory and index_files)

def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list:
    """Split text into overlapping word-count chunks for indexing."""
    words  = text.split()
    chunks = []
    for i in range(0, len(words), chunk_size - overlap):
        chunk = " ".join(words[i : i + chunk_size])
        if chunk:
            chunks.append(chunk)
    return chunks


def index_memory(
    text: str,
    source_path: str = "unknown",
    chunk_size: int = 500,
    overlap: int = 50,
) -> list:
    """
    Chunk text and append entries to indexed_memory.txt.
    All entries carry the 'indexed' tag so retrieve() applies the higher
    threshold and separate budget, keeping code out of personal memory.
    """
    chunks = chunk_text(text, chunk_size, overlap)
    written = []
    try:
        with open(INDEX_FILE, "a", encoding="utf-8") as fh:
            for chunk in chunks:
                line = f"[project][indexed,{source_path}][3] {chunk}"
                fh.write(line + "\n")
                written.append(line)
        return written
    except OSError as exc:
        return [f"[ERROR indexing memory: {exc}]"]


#  TOOL DESCRIPTIONS

TOOLS_DESCRIPTION = [
    {
        "type": "function",
        "function": {
            "name": "run_code",
            "description": (
                "Execute shell commands inside the Termux environment. "
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
                "Fetch a webpage and convert meaningful HTML into structured markdown. "
                "Preserves headings, paragraphs, links, and image/media URLs. "
                "Optionally target a specific element with a CSS selector. "
                "Use cases: google search, retrieve a site's text and a lot."
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
                        "description": "Optional CSS selector to filter content (e.g., 'main', 'article').",
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
                "the assistant. If relevant, sleep mode exits and returns "
                "the detected speech as the next user prompt."
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
                "Use this to communicate what you are currently doing before a result is ready. "
                "Best for: tool call announcements, progress updates, multi-step reasoning checkpoints. "
                "Do NOT use for final answers. Just return those as your response."
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
                        "description": "The destination phone number (with country code, e.g., '919876543210') or contact ID.",
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
            "description": "List all WhatsApp chats and groups with their names, JIDs, unread counts, and metadata. Use this to discover JIDs before setting filters, ignoring contacts/groups, or sending messages to someone whose number you don't know.",
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
            "description": "Silence auto-replies to a specific contact or group for a given number of hours. Use when someone asks Orion to stop replying, or when you want to manually pause replies. Pass hours=0 to lift an existing silence immediately.",
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
            "description": "React to a specific WhatsApp message with an emoji (e.g. 👍 ❤️ 😂). Use the messageId from a received message.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "The serialized message ID to react to."},
                    "emoji":      {"type": "string", "description": "The emoji to react with, e.g. '👍' or '❤️'."},
                },
                "required": ["message_id", "emoji"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_whatsapp_contact_info",
            "description": "Fetch profile information for a WhatsApp contact: display name, phone number, about/status text, and profile picture URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "jid": {"type": "string", "description": "WhatsApp JID of the contact (e.g. '919876543210@c.us')."},
                },
                "required": ["jid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_whatsapp_group_participants",
            "description": "List all participants in a WhatsApp group along with their roles (admin, member). Requires a group JID ending in @g.us.",
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
            "description": "Download the media file from a WhatsApp message (image, video, audio, document, sticker). Returns the base64 data and mimetype. Only call this when the user explicitly asks to see/save a file — do not call automatically on every media message.",
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
            "description": "Schedule a WhatsApp message to be sent automatically at a specific future time. Useful for reminders, follow-ups, or timed announcements.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to":      {"type": "string", "description": "Recipient JID or phone number."},
                    "message": {"type": "string", "description": "The message text to send."},
                    "send_at": {"type": "string", "description": "ISO 8601 datetime string for when to send, e.g. '2026-06-06T09:00:00'."},
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
            "description": "Mark a WhatsApp chat as read, clearing the unread message count on the phone.",
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
                        "description": "The phone number (e.g., '91XXXXXXXXXX') or contact ID to fetch chat history for.",
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
                        "description": "The instructions for generating auto-replies, or empty to use the default instruction.",
                    },
                    "exclude_all_groups_except": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of group names or JIDs to include; all other groups will be excluded from auto-replies.",
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
            "description": "Get a full report of all WhatsApp messages received and sent during busy mode (or since last cleared). Shows who messaged, what they said, and what auto-replies were sent.",
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
            "description": "Set personal context about the user that Orion will always include in its WhatsApp auto-reply system prompt. Use this to tell Orion the user's name, occupation, location, or any context that helps replies feel more personal and accurate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "description": "A plain text description of the user. E.g. The user's name, where he/she lives etc, why he/she is busy stc.",
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
                "Retrieve the full raw conversation chunk or subchunk by its stable ID. "
                "Use when you need detailed history from an earlier turn. "
                "Call list_chunks first to find available IDs. "
                "Parent IDs are integers (e.g. 3). Subchunk IDs are strings (e.g. '3.1'). "
                "If a parent was split, this returns the split index with subchunk references."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chunk_id": {
                        "type": "string",
                        "description": (
                            "The stable chunk ID to retrieve. "
                            "Use '3' for parent chunk 3, or '3.1' for its first subchunk."
                        )
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
            "description": (
                "List all stored conversation chunks with their IDs and one-line summaries. "
                "Use this to find which chunk ID to pass to retrieve_chunk."
            ),
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
            "description": "Run a system diagnosis to check battery, weather, storage, memory, network, and datetime information. Returns a structured dictionary of current system status.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]


#  TOOL FUNCTIONS

def run_code(bash: str, timeout: int = 0) -> str:
    """Execute shell commands in Termux after permission validation."""
    log_write(f"[run_code] {bash}")

    allowed, reason = validate_command(bash)
    if not allowed:
        out = f"[BLOCKED] {reason}"
        log_write(f"[OUT] {out}")
        return out

    printable = bash if len("\n".join(bash.splitlines()[:PRINT_LINE_THRESHOLD])) < PRINT_CHAR_THRESHOLD else bash[:PRINT_CHAR_THRESHOLD] + "\n    .\n    .\n    ."
    try:
        print(f"{GRAY}[EXECUTING] {printable}{RESET}")

        result = subprocess.run(
            bash,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=None if timeout == 0 else timeout,
        )

        out = result.stdout.strip()
        err = result.stderr.strip()

        printable_out = out if len("\n".join(out.splitlines()[:PRINT_LINE_THRESHOLD])) < PRINT_CHAR_THRESHOLD else out[:PRINT_CHAR_THRESHOLD] + "\n    .\n    .\n    ."

        if err and out:
            print(f"{GRAY}[OUT]\n{printable_out}\n{RED}[ERR]\n{err}{RESET}")
            log_write(f"[OUT]\n{out}")
            log_write(f"[ERR]\n{err}")
            return out + "\n[ERR]\n" + err

        if err:
            print(f"{RED}[ERR]\n{err}{RESET}")
            log_write(f"[ERR]\n{err}")
            return "[ERR]\n" + err

        print(f"{GRAY}[OUT]\n{printable_out}{RESET}")
        log_write(f"[OUT]\n{out}")
        return out

    except subprocess.TimeoutExpired:
        msg = f"[TIMEOUT] Command exceeded {timeout} seconds"
        print(f"{RED}{msg}{RESET}")
        log_write(msg)
        return msg

    except Exception as e:
        msg = f"[EXCEPTION] {e}"
        print(f"{RED}[EXCEPTION]\n{e}{RESET}")
        log_write(msg)
        return msg


def save_memory(text: str, type_: str, tags: str, priority: int) -> str:
    """
    Validate and persist a structured memory entry to memories.txt.

    Guards (reject with explanation, never silently drop):
      text > 1000 chars      -> redirect to index_files
      looks like code        -> redirect to index_files
      looks like a log line  -> rejected

    Priority 1-10: 10 = critical behavioral rule, 7-9 = strong preference,
    5-6 = useful fact.
    """
    log_write(f"[save_memory] type:{type_}, tags:{tags}, priority:{priority}, text:{text[:80]}")

    text = text.strip()
    if not text:
        return "[MEMORY_FILTERED: Empty text.]"

    if len(text) > 1000:
        return "[MEMORY_FILTERED: Text too long -- use index_files for bulk content.]"
    if "def " in text or "class " in text or "import " in text:
        return "[MEMORY_FILTERED: Looks like code -- use index_files instead.]"
    if "log.txt" in text or ("error" in text.lower() and "traceback" in text.lower()):
        return "[MEMORY_FILTERED: Looks like a log entry -- not stored in primary memory.]"

    type_    = (type_ or "fact").strip().lower()
    tags     = (tags or "").strip().lower()
    priority = max(1, min(10, int(priority)))

    line = f"[{type_}][{tags}][{priority}] {text}"

    try:
        with open(MEMORY_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(f"{GRAY}[MEMORY SAVED] {line}{RESET}")
        log_write("[OK]")
        return f"Memory saved: {line}"
    except OSError as exc:
        err = f"[ERROR saving memory: {exc}]"
        print(f"{RED}{err}{RESET}")
        log_write(f"[ERR] {exc}")
        return err


def retrieve_memory(query: str, top_k: int = 5) -> str:
    """
    Search both memories.txt and indexed_memory.txt; return labelled results.
    Each line is prefixed [memory][type] or [indexed][type] so the model
    knows the provenance of every item.
    """
    log_write(f"[retrieve_memory] query:{query}, top_k:{top_k}")

    keywords = sorted(_tokenize(query))
    print(f"{GRAY}[MEMORY] retrieving for: {', '.join(keywords) or '(none)'}{RESET}")

    hits = retrieve_flat(query, top_k=top_k)

    if not hits:
        log_write("[EMPTY]")
        return "No relevant memories found."

    lines = []
    for entry in hits:
        source = "indexed" if entry.is_indexed else "memory"
        lines.append(f"[{source}][{entry.type}] {entry.text}")

    out = "\n".join(lines)
    log_write(f"[OUT]\n{out}")
    return out


def read_file(
    path: str,
    segment_start: int = None,
    segment_end: int = None,
    unit: str = "lines",
) -> str:
    """
    Read a file using pure Python — no shell required.

    Full read
        read_file("/path/to/file")

    Segmented read — lines (1-indexed, inclusive on both ends)
        read_file("/path/to/file", segment_start=10, segment_end=25)
        Returns lines 10-25 prefixed with their line numbers.

    Segmented read — bytes
        read_file("/path/to/file", segment_start=0, segment_end=512, unit="bytes")
        Returns the decoded byte slice.

    Parameters
    ----------
    path           : target file (~ expanded)
    segment_start  : first line (1-indexed) or first byte offset; None = start of file
    segment_end    : last line (inclusive) or last byte offset; None = end of file
    unit           : "lines" (default) or "bytes"
    """
    path = os.path.expanduser(path)

    if not os.path.exists(path):
        return f"[ERROR] File not found: {path}"
    if not os.path.isfile(path):
        return f"[ERROR] Path is not a file: {path}"

    log_write(f"[read_file] path:{path} start:{segment_start} end:{segment_end} unit:{unit}")
    print(f"{GRAY}[READING] {path}{RESET}")

    try:
        # Byte segment
        if unit == "bytes":
            with open(path, "rb") as fh:
                if segment_start is not None:
                    fh.seek(segment_start)
                chunk = fh.read(
                    None if segment_end is None else (segment_end - (segment_start or 0))
                )
            text = chunk.decode("utf-8", errors="replace")
            size = os.path.getsize(path)
            header = f"[FILE] {path}  bytes {segment_start or 0}–{segment_end or size}\n"
            return header + text

        # Full or line-segment read
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()

        total = len(all_lines)

        if segment_start is None and segment_end is None:
            # Full file — return with line numbers
            numbered = [f"{i+1:6d}  {ln}" for i, ln in enumerate(all_lines)]
            header   = f"[FILE] {path}  ({total} lines)\n"
            return header + "".join(numbered)

        # Clamp to valid range
        lo = max(1, segment_start or 1) - 1          # convert to 0-indexed
        hi = min(total, segment_end or total)         # inclusive end, 1-indexed

        if lo >= total:
            print(f"{RED}[ERROR] segment_start ({segment_start}) is beyond the file ({total} lines).{RESET}")
            return f"[ERROR] segment_start ({segment_start}) is beyond the file ({total} lines)."
        if lo >= hi:
            print(f"{RED}[ERROR] segment_start ({segment_start}) must be less than segment_end ({segment_end}).{RESET}")
            return f"[ERROR] segment_start ({segment_start}) must be less than segment_end ({segment_end})."

        selected = all_lines[lo:hi]
        numbered = [f"{lo+i+1:6d}  {ln}" for i, ln in enumerate(selected)]
        header   = f"[FILE] {path}  lines {lo+1}–{hi} of {total}\n"
        out = header + "".join(numbered)
        printable_out = out if len("\n".join(out.splitlines()[:PRINT_LINE_THRESHOLD])) < PRINT_CHAR_THRESHOLD else out[:PRINT_CHAR_THRESHOLD] + "\n    .\n    .\n    ."
        print(f"{GRAY}[OUT]\n{printable_out}{RESET}")
        return out

    except OSError as exc:
        log_write(f"[ERR] {exc}")
        print(f"{RED}[ERR] {exc}{RESET}")
        return f"[ERROR] {exc}"


def write_file(
    path: str,
    content: str,
    mode: str = "overwrite",
    segment_start: int = None,
    segment_end: int = None,
    unit: str = "lines",
) -> str:
    """
    Write content to a file using pure Python — no shell required.
    Parent directories are created automatically.

    Modes
    -----
    overwrite  : replace the entire file with content  (default)
    append     : add content after the last byte of the file
    prepend    : insert content before the first byte of the file
    segment    : replace lines segment_start..segment_end (1-indexed, inclusive)
                 — or a byte range when unit='bytes' — with content.
                 Lines outside the segment are untouched.
                 If the replacement content contains newlines, each line
                 is inserted as its own line.

    Parameters
    ----------
    path           : target file (~ expanded; created if absent)
    content        : text to write
    mode           : "overwrite" | "append" | "prepend" | "segment"
    segment_start  : first line (1-indexed) or first byte; required for mode='segment'
    segment_end    : last line (inclusive) or last byte; required for mode='segment'
    unit           : "lines" (default) or "bytes"
    """
    path = os.path.expanduser(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    log_write(f"[write_file] path:{path} mode:{mode} start:{segment_start} end:{segment_end} unit:{unit}")
    print(f"{GRAY}[WRITING] {path}  (mode={mode}){RESET}")

    try:
        # overwrite
        if mode == "overwrite":
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            lines_written = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            print(f"{GRAY}[OK] Wrote {lines_written} line(s) to {path}.{RESET}")
            return f"[OK] Wrote {lines_written} line(s) to {path}."

        # append
        if mode == "append":
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(content)
            print(f"{GRAY}[OK] Appended {len(content)} byte(s) to {path}.{RESET}.")
            return f"[OK] Appended {len(content)} byte(s) to {path}."

        # prepend
        if mode == "prepend":
            existing = ""
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    existing = fh.read()
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content + existing)
            
            print(f"{GRAY}[OK] Prepended {len(content)} byte(s) to {path}.{RESET}")
            return f"[OK] Prepended {len(content)} byte(s) to {path}."

        # segment
        if mode == "segment":
            if segment_start is None or segment_end is None:
                print(f"{GRAY}[ERROR] mode='segment' requires both segment_start and segment_end.{RESET}")
                return "[ERROR] mode='segment' requires both segment_start and segment_end."
            if segment_start < 1:
                print(f"{GRAY}[ERROR] segment_start must be >= 1.{RESET}")
                return "[ERROR] segment_start must be >= 1."
            if segment_end < segment_start:
                print(f"{GRAY}[ERROR] segment_end must be >= segment_start.")
                return "[ERROR] segment_end must be >= segment_start."

            # byte segment
            if unit == "bytes":
                existing = b""
                if os.path.exists(path):
                    with open(path, "rb") as fh:
                        existing = fh.read()
                replacement = content.encode("utf-8")
                new_bytes   = existing[:segment_start] + replacement + existing[segment_end:]
                with open(path, "wb") as fh:
                    fh.write(new_bytes)
                
                print(f"{GRAY}[OK] Replaced bytes {segment_start}-{segment_end} in {path} with {len(replacement)} byte(s).{RESET}")
                return (
                    f"[OK] Replaced bytes {segment_start}-{segment_end} in {path} "
                    f"with {len(replacement)} byte(s)."
                )

            # line segment
            existing_lines = []
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    existing_lines = fh.readlines()

            total = len(existing_lines)
            lo    = segment_start - 1                   # 0-indexed
            hi    = min(segment_end, total)             # exclusive slice end

            if lo > total:
                print(f"{RED}[ERROR] segment_start ({segment_start}) is beyond the file length ({total} lines).{RESET}")
                return (
                    f"[ERROR] segment_start ({segment_start}) is beyond "
                    f"the file length ({total} lines)."
                )

            # Ensure content ends with newline so surrounding lines stay intact
            if content and not content.endswith("\n"):
                content += "\n"

            new_lines = existing_lines[:lo] + [content] + existing_lines[hi:]
            with open(path, "w", encoding="utf-8") as fh:
                fh.writelines(new_lines)

            replaced = hi - lo
            print(f"{GRAY}[OK] Replaced line(s) {segment_start}-{min(segment_end, total)} ({replaced} line(s) removed, replacement written) in {path}.{RESET}")
            return (
                f"[OK] Replaced line(s) {segment_start}-{min(segment_end, total)} "
                f"({replaced} line(s) removed, replacement written) in {path}."
            )
        
        print(f"{GRAY}[ERROR] Unknown mode '{mode}'. Use: overwrite, append, prepend, segment.{RESET}")
        return f"[ERROR] Unknown mode '{mode}'. Use: overwrite, append, prepend, segment."

    except OSError as exc:
        log_write(f"[ERR] {exc}")
        print(f"{RED}[ERR] {exc}{RESET}")
        return f"[ERROR] {exc}"


def index_files(path: str, extension_filter: str = "") -> str:
    """Read files, chunk them, and store in indexed_memory.txt (not memories.txt)."""
    printable_index = ""
    log_write(f"[index_files] path:{path}, filter:{extension_filter}")

    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return f"[ERROR] Path does not exist: {path}"

    # Default extensions when no filter is provided
    DEFAULT_EXTENSIONS = {
        ".txt", ".md", ".py", ".sh", ".json",
        ".yaml", ".yml", ".xml", ".html",
        ".css", ".js", ".java", ".c",
        ".cpp", ".h", ".hpp"
    }

    if extension_filter.strip():
        extensions = {
            ext.strip().lower()
            if ext.strip().startswith(".")
            else "." + ext.strip().lower()
            for ext in extension_filter.split(",")
            if ext.strip()
        }
    else:
        extensions = DEFAULT_EXTENSIONS

    indexed_files_count = 0
    indexed_chunks_count = 0

    files_to_process = []

    if os.path.isfile(path):
        files_to_process.append(path)

    else:
        for root, dirs, files in os.walk(path):

            # Directory exclusions
            dirs[:] = [
                d for d in dirs
                if d not in {"node_modules", ".git", "__pycache__"}
            ]

            for file in files:
                full_path = os.path.join(root, file)

                if Path(full_path).suffix.lower() in extensions:
                    files_to_process.append(full_path)

    for fpath in files_to_process:
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()

            if not text.strip():
                continue

            rel_path = os.path.relpath(
                fpath,
                os.path.dirname(path) if os.path.isdir(path) else os.path.dirname(fpath)
            )

            chunks = chunk_text(text)

            if not chunks:
                continue

            result = index_memory(text, source_path=rel_path)

            if result and not result[0].startswith("[ERROR"):
                indexed_files_count += 1
                indexed_chunks_count += len(chunks)

                if len(printable_index) < PRINT_CHAR_THRESHOLD:
                    printable_index += f"[INDEXED] {rel_path} ({len(chunks)}"

        except Exception as e:
            print(f"Failed to index {fpath}: {e}")
            
    printable_index = printable_index if len(printable_index) <= PRINT_CHAR_THRESHOLD else printable_index[:PRINT_CHAR_THRESHOLD]+"\n    .\n    .\n    ."
    print(f"{GRAY}{printable_index}{RESET}")
    return (
        f"Successfully indexed "
        f"{indexed_chunks_count} chunks "
        f"from {indexed_files_count} file(s) "
        f"into indexed_memory.txt."
    )


def web_scrape(url: str, selector: str = None) -> str:
    """Fetch a webpage and convert the readable parts into markdown-like text."""
    try:
        print(f"{GRAY}[SCRAPING] {url}{RESET}")
        log_write(f"[web_scrape] URL:{url} selector:{selector}")

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/91.0 Safari/537.36"
            )
        }

        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" not in content_type:
            msg = f"[ERROR] Unsupported content type: {content_type}"
            log_write(msg)
            return msg

        soup = BeautifulSoup(response.text, "html.parser")

        for tag in soup.select(
            "script, style, noscript, nav, footer, aside, "
            ".sidebar, .menu, .ads, .popup, .cookie, .banner"
        ):
            tag.decompose()

        root = soup.select_one(selector) if selector else soup
        if selector and root is None:
            msg = f"[ERROR] Selector '{selector}' not found."
            log_write(msg)
            return msg

        lines    = []
        seen_urls: set = set()

        def resolve(raw: str) -> str:
            return urljoin(url, raw.strip())

        def add_line(text: str):
            text = html.unescape(text).strip()
            if text:
                lines.append(text)

        def label_for(tag: Tag) -> str:
            for attr in ("alt", "title", "aria-label", "data-label", "data-title"):
                value = tag.get(attr)
                if value:
                    return str(value).strip()
            return ""

        def link_markdown(tag: Tag) -> str:
            text = tag.get_text(" ", strip=True)
            href = tag.get("href")
            if not href:
                return text
            full = resolve(href)
            if full in seen_urls:
                return text or f"<{full}>"
            seen_urls.add(full)
            return f"[{text}]({full})" if text else f"<{full}>"

        def image_markdown(tag: Tag) -> str:
            alt = label_for(tag)
            src = tag.get("src") or tag.get("data-src") or tag.get("data-original")
            if not src:
                return alt
            return f"![{alt or 'image'}]({resolve(src)})"

        def media_markdown(tag: Tag) -> str:
            label = label_for(tag)
            src   = tag.get("src") or tag.get("poster") or tag.get("data-src")
            if not src:
                return label
            full = resolve(src)
            return f"[{label}]({full})" if label else f"<{full}>"

        def render_inline(node) -> str:
            parts = []
            for child in node.children:
                if isinstance(child, NavigableString):
                    parts.append(html.unescape(str(child)))
                    continue
                if not isinstance(child, Tag):
                    continue
                name = child.name.lower()
                if name == "a":
                    parts.append(link_markdown(child))
                elif name == "img":
                    parts.append(image_markdown(child))
                elif name in {"video", "audio", "source", "iframe", "embed"}:
                    parts.append(media_markdown(child))
                elif name == "br":
                    parts.append("\n")
                else:
                    parts.append(render_inline(child))
            return " ".join("".join(parts).split()).strip()

        def walk(node):
            for child in node.children:

                # Ignore raw text nodes here.
                # Semantic tags handle their own text extraction.
                if isinstance(child, NavigableString):
                    continue

                if not isinstance(child, Tag):
                    continue

                name = child.name.lower()

                # Headings
                if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                    heading = render_inline(child)
                    if heading:
                        add_line("#" * int(name[1]) + " " + heading)
                        lines.append("")

                # List items
                elif name == "li":
                    item = render_inline(child)
                    if item:
                        add_line(f"- {item}")

                # Paragraph-like semantic content
                elif name in {"p", "blockquote"}:
                    inner = render_inline(child)
                    if inner:
                        add_line(inner)
                        lines.append("")

                # Code blocks
                elif name == "pre":
                    code_text = child.get_text("\n", strip=True)
                    if code_text:
                        add_line("```")
                        add_line(code_text)
                        add_line("```")
                        lines.append("")

                # Structural/container elements
                elif name in {
                    "html",
                    "body",
                    "article",
                    "section",
                    "main",
                    "div",
                    "ul",
                    "ol",
                    "header",
                    "footer",
                    "aside",
                    "nav",
                }:
                    walk(child)

                # Images/media outside inline contexts
                elif name == "img":
                    img = image_markdown(child)
                    if img:
                        add_line(img)

                elif name in {"video", "audio", "iframe", "embed"}:
                    media = media_markdown(child)
                    if media:
                        add_line(media)

                # Fallback recursion
                else:
                    walk(child)

        walk(root)

        # Collapse consecutive blank lines
        cleaned    = []
        prev_blank = False
        for line in lines:
            line = line.rstrip()
            if not line:
                if not prev_blank:
                    cleaned.append("")
                prev_blank = True
            else:
                cleaned.append(line)
                prev_blank = False

        text = "\n".join(cleaned).strip()
        if len(text) > 12000:
            text = text[:12000] + "\n\n... (content truncated)"

        log_write("[DONE]")
        return text

    except Exception as e:
        print(f"{RED}[SCRAPE FAILED] {e}{RESET}")
        log_write(f"[ERROR] {e}")
        return f"[ERROR] Scraping failed: {e}"


_speak_thread: threading.Thread | None = None

def speak(text: str, debug: bool = False, block: bool = False) -> None:
    global _speak_thread

    if _speak_thread and _speak_thread.is_alive():
        _speak_thread.join()

    _speak_thread = threading.Thread(
        target=_speak_blocking,
        args=(text, debug),
        daemon=True,
    )
    _speak_thread.start()

    if block:
        _speak_thread.join()


def _speak_blocking(text: str, debug: bool = False) -> str:
    if debug:
        print("speaking")

    safe_text = shlex.quote(render_for_voice(text))
    cmd = (
        f"edge-tts "
        f'--voice "en-US-AndrewNeural" '
        f"--text {safe_text} "
        f"--write-media - | mpv -"
    )
    process = None
    try:
        process = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        stdout, stderr = process.communicate()
        if debug:
            print(process)
            if stdout:
                print(stdout)
            if stderr:
                print(stderr)
        if stderr and stderr.strip():
            return stderr.strip()
        return "OK"

    except KeyboardInterrupt:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGINT)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        print("\nInterrupted")
        return "Interrupted"

    except Exception as e:
        if debug:
            print(e)
        return f"[EXCEPTION] {e}"


def sleep_mode() -> None:
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
        if not os.path.isdir(os.path.join(BASE_DIR, "Termux-STT")):
            raise Exception("Termux-STT not found.")
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
        try:
            relevant = is_wake_relevant(heard)
            if relevant:
                print(f"{GRAY}[WAKING UP]{RESET}")
                return heard
            else:
                print(f"{GRAY}[IGNORED]{RESET}")
        except Exception as e:
            print(f"{RED}[WAKE CHECK FAILED] {e}{RESET}")
            

def intermediate_print(text: str, voice: bool = False) -> None:
    print("AI (Intermediate) >")
    print(render_markdown_terminal(text))
    print()
    if voice:
        speak(render_for_voice(text))
        

def _check_battery() -> dict:
    try:
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
        result = subprocess.run(
            ["df", "-h", os.path.expanduser("~")],
            capture_output=True, text=True, timeout=8
        )
        lines = result.stdout.strip().splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            return {
                "total":       parts[1],
                "used":        parts[2],
                "available":   parts[3],
                "use_percent": parts[4],
            }
        return {"error": "unexpected df output"}
    except Exception as e:
        return {"error": str(e)}


def _check_memory() -> dict:
    try:
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



# --- WHATSAPP CORE TOOLS ---

def send_whatsapp_message(to_phone: str, message_text: str) -> str:
    """Send a WhatsApp message to a specific phone number or contact ID."""
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
    """Get the current status of the WhatsApp bot client and any pending received messages."""
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
    """List all WhatsApp chats and groups with JIDs, names, unread counts, and metadata."""
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
    """Silence auto-replies to a contact/group for N hours. hours=0 lifts immediately."""
    log_write(f"[silence_whatsapp_contact] jid:{jid} hours:{hours}")
    print(f"{GRAY}[WhatsApp] Silencing {jid} for {hours} hour(s)...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    ok, msg = whatsapp_manager.silence_contact(jid, hours=hours)
    return msg


def react_to_whatsapp_message(message_id: str, emoji: str) -> str:
    """React to a WhatsApp message with an emoji."""
    log_write(f"[react_to_whatsapp_message] id:{message_id} emoji:{emoji}")
    print(f"{GRAY}[WhatsApp] Reacting to message {message_id} with {emoji}...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    ok = whatsapp_manager.react(message_id, emoji)
    return f"Reacted with {emoji}." if ok else "Failed to react — message may no longer be available."


def get_whatsapp_contact_info(jid: str) -> str:
    """Fetch profile info for a WhatsApp contact."""
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
    """List all participants of a WhatsApp group with their roles."""
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
    """Download and save media from a WhatsApp message to /tmp."""
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
    out_path = f"/tmp/{filename}"
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(result["data"]))
    size_kb = os.path.getsize(out_path) // 1024
    return f"Saved to {out_path} ({size_kb} KB, {mimetype})"


def schedule_whatsapp_message(to: str, message: str, send_at: str) -> str:
    """Schedule a WhatsApp message to be sent at a specific ISO datetime."""
    log_write(f"[schedule_whatsapp_message] to:{to} send_at:{send_at}")
    print(f"{GRAY}[WhatsApp] Scheduling message to {to} at {send_at}...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    ok, info = whatsapp_manager.schedule_message(to, message, send_at)
    return info if ok else f"Failed to schedule: {info}"


def search_whatsapp_chat(jid: str, query: str, limit: int = 20) -> str:
    """Search for messages containing a keyword in a specific chat."""
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
    """Archive or unarchive a WhatsApp chat."""
    log_write(f"[archive_whatsapp_chat] jid:{jid} archive:{archive}")
    print(f"{GRAY}[WhatsApp] {'Archiving' if archive else 'Unarchiving'} chat {jid}...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    ok = whatsapp_manager.archive_chat(jid, archive=archive)
    action = "Archived" if archive else "Unarchived"
    return f"{action} {jid}." if ok else f"Failed to {'archive' if archive else 'unarchive'} {jid}."


def set_whatsapp_seen(jid: str) -> str:
    """Mark a WhatsApp chat as read, clearing the unread count on the phone."""
    log_write(f"[set_whatsapp_seen] jid:{jid}")
    print(f"{GRAY}[WhatsApp] Marking chat {jid} as read...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    whatsapp_manager.set_seen(jid)
    return f"Marked {jid} as read."


def get_pending_whatsapp_messages(clear: bool = True) -> str:
    """Retrieve and clear any pending received WhatsApp messages from the background queue."""
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
    """Fetch the recent chat message history timeline for a specific phone number or contact ID from WhatsApp."""
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
    """Enable or disable auto-reply 'busy' mode with a specific instruction and optional group exclusions."""
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
    """Set personal context about the user injected into every Orion auto-reply."""
    log_write(f"[set_whatsapp_user_profile] {profile}")
    print(f"{GRAY}[WhatsApp] Updating user profile context...{RESET}")
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    whatsapp_manager.set_user_profile(profile)
    print(f"{GRAY}[WhatsApp] User profile updated.{RESET}")
    return f'User profile set: "{profile}"'


def get_whatsapp_report(clear: bool = False) -> str:
    """Read whatsapp_log.jsonl and return a human-readable conversation report."""
    log_write(f"[get_whatsapp_report] clear:{clear}")
    print(f"{GRAY}[WhatsApp] Generating conversation report (clear={clear})...{RESET}")
    
    if not WP_AVAILABLE:
        return "Termux-WP not available. Probably Termux-WP not installed."
    
    try:
        if not os.path.exists(WA_LOG_FILE):
            return "No WhatsApp log file found. No conversations have been recorded yet."

        with open(WA_LOG_FILE, "r", encoding="utf-8") as fh:
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

        # Group by sender_id for a per-contact summary
        from collections import defaultdict
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
            open(WA_LOG_FILE, "w", encoding="utf-8").close()
            report_lines.append(" Log cleared.")

        return "\n".join(report_lines)

    except Exception as e:
        return f"[ERROR] Failed to read WhatsApp report: {e}"
