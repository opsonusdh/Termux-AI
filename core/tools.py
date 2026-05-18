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
import subprocess
from pathlib import Path
from openai import OpenAI
from urllib.parse import urljoin
from collections import defaultdict
from bs4 import BeautifulSoup, NavigableString, Tag

from permissions import validate_command
from renderer import RED, GRAY, RESET, render_for_voice

WAKE_WORDS = ["orion", "orien", "orian"]
PRINT_LINE_THRESHOLD = 20
PRINT_CHAR_THRESHOLD = 500
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

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE  = os.path.join(BASE_DIR, "log.txt")

if not os.path.exists(LOG_FILE):
    open(LOG_FILE, "a", encoding="utf-8").close()


def log_write(message: str) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(message.rstrip("\n") + "\n")


#  MEMORY STORE

#  Paths

MEMORY_FILE = os.path.join(BASE_DIR, "memories.txt")       # personal / conversational
INDEX_FILE  = os.path.join(BASE_DIR, "indexed_memory.txt") # bulk file / code chunks

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


def make_client(key):
    return OpenAI(
        api_key=key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )

def ask_ai_simple(prompt: str, _model, _sys_prompt,) -> str:
    ind = 0
    api_keys_len = len(API_KEYS)
    while True:
        client = make_client(API_KEYS[ind])
        try:
            response = client.chat.completions.create(
                model=_model,
                messages=[
                    {
                        "role": "system",
                        "content": _sys_prompt
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
            )
            msg = response.choices[0].message

            if msg.content:
                return msg.content.strip()

            return "[EMPTY RESPONSE]"

        except Exception as e:
            msg_str = str(e)

            if (
                "503" in msg_str
                or "UNAVAILABLE" in msg_str
                or "overloaded" in msg_str.lower()
                or "429" in msg_str
                or "RESOURCE_EXHAUSTED" in msg_str
                or "rate limit" in msg_str.lower()
            ):

                print(f"{RED}Model overloaded.{RESET}")
                time.sleep(5)
            elif "API_KEY_INVALID" in msg_str:
                print(f"{RED}Invalid API key.{RESET}")
            else:
                raise

            ind += 1
            if ind >= api_keys_len:
                ind = 0
                
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
                "Optionally target a specific element with a CSS selector."
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
            return f"[ERROR] segment_start ({segment_start}) is beyond the file ({total} lines)."
        if lo >= hi:
            return f"[ERROR] segment_start ({segment_start}) must be less than segment_end ({segment_end})."

        selected = all_lines[lo:hi]
        numbered = [f"{lo+i+1:6d}  {ln}" for i, ln in enumerate(selected)]
        header   = f"[FILE] {path}  lines {lo+1}–{hi} of {total}\n"
        return header + "".join(numbered)

    except OSError as exc:
        log_write(f"[ERR] {exc}")
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

    try:
        # overwrite
        if mode == "overwrite":
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            lines_written = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            return f"[OK] Wrote {lines_written} line(s) to {path}."

        # append
        if mode == "append":
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(content)
            return f"[OK] Appended {len(content)} byte(s) to {path}."

        # prepend
        if mode == "prepend":
            existing = ""
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    existing = fh.read()
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content + existing)
            return f"[OK] Prepended {len(content)} byte(s) to {path}."

        # segment
        if mode == "segment":
            if segment_start is None or segment_end is None:
                return "[ERROR] mode='segment' requires both segment_start and segment_end."
            if segment_start < 1:
                return "[ERROR] segment_start must be >= 1."
            if segment_end < segment_start:
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
                return (
                    f"[OK] Replaced bytes {segment_start}–{segment_end} in {path} "
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
            return (
                f"[OK] Replaced line(s) {segment_start}–{min(segment_end, total)} "
                f"({replaced} line(s) removed, replacement written) in {path}."
            )

        return f"[ERROR] Unknown mode '{mode}'. Use: overwrite, append, prepend, segment."

    except OSError as exc:
        log_write(f"[ERR] {exc}")
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
                if isinstance(child, NavigableString):
                    txt = str(child).strip()
                    if txt:
                        add_line(txt)
                    continue
                if not isinstance(child, Tag):
                    continue
                name = child.name.lower()

                if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                    heading = render_inline(child)
                    if heading:
                        add_line("#" * int(name[1]) + " " + heading)
                        lines.append("")
                elif name == "li":
                    item = render_inline(child)
                    if item:
                        add_line(f"- {item}")
                elif name in {"ul", "ol"}:
                    walk(child)
                    lines.append("")
                elif name in {"p", "article", "section", "main", "div",
                               "header", "footer", "aside", "blockquote"}:
                    inner = render_inline(child)
                    if inner:
                        add_line(inner)
                        lines.append("")
                    else:
                        walk(child)
                elif name == "pre":
                    code_text = child.get_text("\n", strip=True)
                    if code_text:
                        add_line("```")
                        add_line(code_text)
                        add_line("```")
                        lines.append("")
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


def speak(text: str, debug: bool = False) -> str:
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


def sleep_mode():
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
        heard = listen(once=True, cleaned=False)

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
            