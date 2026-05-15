import os
import subprocess
import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from urllib.parse import urljoin
import html
import shlex


from permissions import validate_command
from renderer import RED, GRAY, RESET
import memory_store

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LOG_FILE = os.path.join(BASE_DIR, "log.txt")
if not os.path.exists(LOG_FILE):
    subprocess.run(["touch", LOG_FILE], shell=True)

TOOLS_DESCRIPTION = \
[
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
                        "description": "Shell command to execute"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": (
                            "Maximum execution time in seconds before terminating "
                            "the command. Use 0 for no timeout."
                        ),
                        "default": 0,
                        "minimum": 0
                    }
                },
                "required": ["bash"]
            }
        }
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
                    }
                },
                "required": ["url"],
            },
        },
    },
]

def run_code(bash: str, timeout=0) -> str:
    """Execute shell commands in Termux after permission validation."""
    with open(LOG_FILE, "a") as file:
        file.write(f"[run_code] {bash}")
        
    allowed, reason = validate_command(bash)
    if not allowed:
        out = f"[BLOCKED] {reason}"
        with open(LOG_FILE, "a") as file:
            file.write(f"[OUT] {out}")
        return out

    try:
        print(f"{GRAY}[EXECUTING] {bash}{RESET}")
        if timeout != 0:
            result = subprocess.run(
                bash,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout
            )
        else:
            result = subprocess.run(
                bash,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
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
                with open(LOG_FILE, "a") as file:
                    file.write(f"[OUT] {out}")
                    file.write(f"[ERR] {err}")
                return out + "\n[ERR]\n" + err
            print(f"{RED}[ERR]\n{err}{RESET}")
            with open(LOG_FILE, "a") as file:
                file.write(f"[ERR] {err}")
            return "[ERR]\n" + err

        print(f"{GRAY}[OUT]\n{out}{RESET}")
        with open(LOG_FILE, "a") as file:
            file.write(f"[OUT] {out}")
        return out

    except Exception as e:
        print(f"{RED}[EXCEPTION]\n{e}{RESET}")
        with open(LOG_FILE, "a") as file:
             file.write(f"[EXC] {e}")
        return f"[EXCEPTION] {e}"



def save_memory(text: str, type_: str, tags: str, priority: int) -> str:
    """
    Persist a structured memory entry to memories.txt.
    Called by the model when it learns something stable.
    """
    with open(LOG_FILE, "a") as file:
        file.write(f"[save_memory] text:{text}, type:{type_} tags:{tags} priority:{priority}")
    result = memory_store.save_memory(
        text=text,
        type_=type_,
        tags=tags,
        priority=priority,
    )
    if result.startswith("[ERROR"):
        print(f"{RED}[MEMORY SAVE FAILED] {result}{RESET}")
        with open(LOG_FILE, "a") as file:
            file.write(f"[ERR]")
        return result

    print(f"{GRAY}[MEMORY SAVED] {result}{RESET}")
    with open(LOG_FILE, "a") as file:
         file.write(f"[OK]")
    return f"Memory saved: {result}"


def retrieve_memory(query: str, top_k: int = 5) -> str:
    """
    Retrieve relevant memories and return them as formatted text.
    Also prints a grey debug line with the query keywords.
    """
    with open(LOG_FILE, "a") as file:
        file.write(f"[retrive_memory] query:{query}, top_k:{top_k}")
    keywords = sorted(memory_store._tokenize(query))
    keyword_str = ", ".join(keywords) if keywords else "(none)"

    print(f"{GRAY}[MEMORY] retrieving for keywords: {keyword_str}{RESET}")

    hits = memory_store.retrieve(query, top_k=top_k)

    if not hits:
        with open(LOG_FILE, "a") as file:
            file.write(f"[EMPTY]")
        return "No relevant memories found."

    lines = []
    for entry in hits:
        lines.append(f"[{entry.type}] {entry.text}")
    
    with open(LOG_FILE, "a") as file:
        file.write(f"[OUT]\n{'\n'.join(lines)}")
    return "\n".join(lines)



def web_scrape(url: str, selector: str = None) -> str:
    """
    Fetch a webpage and convert the readable parts into markdown.
    """
    try:
        print(f"{GRAY}[SCRAPING] {url}{RESET}")
        with open(LOG_FILE, "a") as file:
            file.write(f"[web_scrape] URL:{url} selector:{selector}")

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

        content_type = response.headers.get("Content-Type", "")
        if "text/html" not in content_type:
            with open(LOG_FILE, "a") as file:
                file.write(f"[ERROR] Unsupported content type: {content_type}")
            return f"[ERROR] Unsupported content type: {content_type}"

        soup = BeautifulSoup(response.text, "html.parser")

        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        root = soup.select_one(selector) if selector else soup
        if selector and not root:
            with open(LOG_FILE, "a") as file:
                file.write(f"[ERROR] Selector '{selector}' not found.")
            return f"[ERROR] Selector '{selector}' not found."

        lines = []
        seen_urls = set()

        def resolve(raw: str) -> str:
            return urljoin(url, raw.strip())

        def add_line(text: str):
            text = html.unescape(text).strip()
            if text:
                lines.append(text)

        def add_url(raw: str):
            if not raw:
                return
            full = resolve(raw)
            if full not in seen_urls:
                seen_urls.add(full)
                lines.append(f"URL: {full}")

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
                    heading = child.get_text(" ", strip=True)
                    if heading:
                        level = int(name[1])
                        add_line("#" * level + " " + heading)
                        lines.append("")
                    continue

                if name == "a":
                    add_line(link_markdown(child))
                    continue

                if name == "img":
                    add_line(image_markdown(child))
                    continue

                if name in {"video", "audio", "source", "iframe", "embed"}:
                    label = label_for(child)
                    media = media_markdown(child)
                    if label and media and label not in media:
                        add_line(f"{label} {media}")
                    else:
                        add_line(media)
                    continue

                if name == "li":
                    item = child.get_text(" ", strip=True)
                    if item:
                        add_line(f"- {item}")
                    for sub in child.find_all(["a", "img", "video", "audio", "source"], recursive=True):
                        if sub.name == "a":
                            add_line(link_markdown(sub))
                        elif sub.name == "img":
                            add_line(image_markdown(sub))
                        else:
                            add_line(media_markdown(sub))
                    continue

                if name in {"p", "article", "section", "main", "div", "header", "footer", "aside"}:
                    inner = child.get_text(" ", strip=True)
                    if inner:
                        add_line(inner)
                        lines.append("")
                    continue

                if name in {"ul", "ol"}:
                    walk(child)
                    lines.append("")
                    continue

                walk(child)

        walk(root)

        # Clean up blank lines
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
        
        with open(LOG_FILE, "a") as file:
            file.write("[DONE]")
        return text

    except Exception as e:
        print(f"{RED}[SCRAPE FAILED] {e}{RESET}")
        with open(LOG_FILE, "a") as file:
            file.write("[ERROR] {e}")
        return f"[ERROR] Scraping failed: {e}"



def speak(text: str, debug=False) -> str:
    if debug:
        print("speaking")

    safe_text = shlex.quote(text)

    cmd = (
        f"edge-tts "
        f'--voice "en-US-AndrewNeural" '
        f"--text {safe_text} "
        f"--write-media - | mpv -"
    )

    out = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True
    )
    if debug:
        print(out)
    if out.stderr:
        return out.stderr.strip()
    return "OK"