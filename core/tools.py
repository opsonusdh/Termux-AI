import os
import signal
import shlex
import html
import subprocess
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

import memory_store
from permissions import validate_command
from renderer import RED, GRAY, RESET, render_for_voice


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(BASE_DIR, "log.txt")

if not os.path.exists(LOG_FILE):
    open(LOG_FILE, "a", encoding="utf-8").close()


def log_write(message: str) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as file:
        file.write(message.rstrip("\n") + "\n")


TOOLS_DESCRIPTION = [
    {
        "type": "function",
        "function": {
            "name": "index_files",
            "description": (
                "Scan a directory and index its files into the RAG memory system. "
                "Useful for learning about a codebase or a set of documents."
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
                        "description": "Comma-separated extensions (e.g., '.py,.md'). If empty, indexes all text files.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_code",
            "description": (
                "Execute shell commands inside the Termux environment. "
                "Supports optional execution timeout in seconds."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "bash": {
                        "type": "string",
                        "description": "Shell command to execute",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": (
                            "Maximum execution time in seconds before terminating "
                            "the command. Use 0 for no timeout."
                        ),
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
                        "description": "Maximum number of relevant memories to return.",
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
            "name": "web_scrape",
            "description": (
                "Fetch a webpage and convert meaningful HTML content into structured markdown. "
                "Preserves headings, paragraphs, links, image/media URLs, alt text, and readable content "
                "while removing scripts, styles, and unnecessary page noise. "
                "Optionally provide a CSS selector to target specific elements."
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
                },
                "required": ["url"],
            },
        },
    },
]


def run_code(bash: str, timeout: int = 0) -> str:
    """Execute shell commands in Termux after permission validation."""
    log_write(f"[run_code] {bash}")

    allowed, reason = validate_command(bash)
    if not allowed:
        out = f"[BLOCKED] {reason}"
        log_write(f"[OUT] {out}")
        return out
    printable_bash = out if  len("\n".join(out.splitlines()[:20])) < 500 else out[:500]+"\n .\n .\n ."
    try:
        print(f"{GRAY}[EXECUTING] {printable_bash}{RESET}")

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

        printable_out = out if  len("\n".join(out.splitlines()[:20])) < 500 else out[:500]+"\n .\n .\n ."

        if err and out:
            print(f"{GRAY}[OUT]\n{printable_out}\n{RED}[ERR]\n{err}{RESET}")
            log_write(f"[OUT]\n{out}")
            log_write(f"[ERR]\n{err}")
            return out + "\n[ERR]\n" + err

        if err:
            print(f"{RED}[ERR]\n{err}{RESET}")
            log_write(f"[ERR]\n{err}")
            return "[ERR]\n" + err

        print(f"{GRAY}[OUT]\n{out}{RESET}")
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
    Persist a structured memory entry to memories.txt.
    Called by the model when it learns something stable.
    """
    log_write(f"[save_memory] text:{text}, type:{type_}, tags:{tags}, priority:{priority}")

    result = memory_store.save_memory(
        text=text,
        type_=type_,
        tags=tags,
        priority=priority,
    )

    if result.startswith("[ERROR"):
        print(f"{RED}[MEMORY SAVE FAILED] {result}{RESET}")
        log_write("[ERR]")
        return result

    print(f"{GRAY}[MEMORY SAVED] {result}{RESET}")
    log_write("[OK]")
    return f"Memory saved: {result}"


def retrieve_memory(query: str, top_k: int = 5) -> str:
    """
    Retrieve relevant memories and return them as formatted text.
    Also prints a grey debug line with the query keywords.
    """
    log_write(f"[retrieve_memory] query:{query}, top_k:{top_k}")

    keywords = sorted(memory_store._tokenize(query))
    keyword_str = ", ".join(keywords) if keywords else "(none)"
    print(f"{GRAY}[MEMORY] retrieving for keywords: {keyword_str}{RESET}")

    hits = memory_store.retrieve(query, top_k=top_k)

    if not hits:
        log_write("[EMPTY]")
        return "No relevant memories found."

    lines = []
    for entry in hits:
        lines.append(f"[{entry.type}] {entry.text}")

    log_write("[OUT]\n" + "\n".join(lines))
    return "\n".join(lines)


def web_scrape(url: str, selector: str = None) -> str:
    """
    Fetch a webpage and convert the readable parts into markdown-like text.
    """
    try:
        print(f"{GRAY}[SCRAPING] {url}{RESET}")
        log_write(f"[web_scrape] URL:{url} selector:{selector}")

        headers = {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
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
            "script, style, noscript, nav, footer, aside, .sidebar, .menu, .ads, .popup, .cookie, .banner"
        ):
            tag.decompose()

        root = soup.select_one(selector) if selector else soup
        if selector and root is None:
            msg = f"[ERROR] Selector '{selector}' not found."
            log_write(msg)
            return msg

        lines = []
        seen_urls = set()

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
            if text:
                return f"[{text}]({full})"
            return f"<{full}>"

        def image_markdown(tag: Tag) -> str:
            alt = label_for(tag)
            src = tag.get("src") or tag.get("data-src") or tag.get("data-original")
            if not src:
                return alt
            full = resolve(src)
            if not alt:
                alt = "image"
            return f"![{alt}]({full})"

        def media_markdown(tag: Tag) -> str:
            label = label_for(tag)
            src = tag.get("src") or tag.get("poster") or tag.get("data-src")
            if not src:
                return label
            full = resolve(src)
            if label:
                return f"[{label}]({full})"
            return f"<{full}>"

        def render_inline(node) -> str:
            parts = []

            for child in node.children:
                if isinstance(child, NavigableString):
                    txt = html.unescape(str(child))
                    if txt:
                        parts.append(txt)
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

            text = "".join(parts)
            text = " ".join(text.split())
            return text.strip()

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
                        level = int(name[1])
                        add_line("#" * level + " " + heading)
                        lines.append("")
                    continue

                if name == "li":
                    item = render_inline(child)
                    if item:
                        add_line(f"- {item}")
                    continue

                if name in {"ul", "ol"}:
                    walk(child)
                    lines.append("")
                    continue

                if name in {"p", "article", "section", "main", "div", "header", "footer", "aside", "blockquote"}:
                    inner = render_inline(child)
                    if inner:
                        add_line(inner)
                        lines.append("")
                    else:
                        walk(child)
                    continue

                if name == "pre":
                    code_text = child.get_text("\n", strip=True)
                    if code_text:
                        add_line("```")
                        add_line(code_text)
                        add_line("```")
                        lines.append("")
                    continue

                walk(child)

        walk(root)

        cleaned = []
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

        MAX_LEN = 12000
        if len(text) > MAX_LEN:
            text = text[:MAX_LEN] + "\n\n... (content truncated)"

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
def index_files(path: str, extension_filter: str = "") -> str:
    """Reads files, chunks them, and saves them to memory."""
    log_write(f"[index_files] path:{path}, filter:{extension_filter}")
    
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return f"[ERROR] Path does not exist: {path}"

    extensions = [ext.strip().lower() for ext in extension_filter.split(",") if ext.strip()]
    
    indexed_count = 0
    
    files_to_process = []
    if os.path.isfile(path):
        files_to_process.append(path)
    else:
        for root, dirs, files in os.walk(path):
            if "node_modules" in root or ".git" in root or "__pycache__" in root:
                continue
            for file in files:
                if not extensions or any(file.lower().endswith(ext) for ext in extensions):
                    files_to_process.append(os.path.join(root, file))

    for fpath in files_to_process:
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            
            rel_path = os.path.relpath(fpath, os.path.dirname(path))
            chunks = memory_store.chunk_text(text)
            
            for i, chunk in enumerate(chunks):
                memory_store.save_memory(
                    text=f"File: {rel_path} (Part {i+1}): {chunk}",
                    type_="fact",
                    tags=f"index,file,source_code,{os.path.basename(fpath)}",
                    priority=5
                )
            indexed_count += 1
        except Exception as e:
            print(f"Failed to index {fpath}: {e}")

    return f"Successfully indexed {indexed_count} files into memory."
