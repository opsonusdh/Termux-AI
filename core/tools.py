import os
import re
import sys
import json
import html
import time
import shlex
import shutil
import signal
import socket
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

# The permission gate is imported as a *package* module.  The other three
# call sites in this file already use `from core import permissions`; importing
# it as a top-level module here loaded a second, independent copy of the same
# file, so the two copies had separate module state and separate monkeypatches
# for the very same permission rules.
from core.permissions import validate_command, request_file_permission
from renderer import RED, GRAY, YELLOW, RESET, render_for_voice, render_markdown_terminal
from core.models import (
    PROVIDERS,
    IMAGE_MODEL_SLOTS,
    get_subagent_rotation,
    normalize_intelligence_level,
)
from core import shell_runner
from core import google_api
from core import google_oauth
from core import runtime_log
from core import display_state
from core import app_config
from core import notifier
# Module-level import (not a `from ... import name`) so this file shares the
# single global PluginManager instance rather than building a second one.
from core import plugin_manager as plugin_manager_module
import paths


# ── Platform wrapper re-exports ────────────────────────────────────────────────
# The `tools/` package (which holds the Termux API wrappers) is shadowed in
# sys.modules by this very module: core/tools.py is imported as "tools", so
# `from tools.wrapper_x import y` cannot resolve from here. Load the wrapper
# modules by file path and re-export their functions so that every consumer of
# `from tools import *` (core/llm_client.py, core/interface.py) keeps working
# and every advertised tool route resolves to a real callable.
#
# Each wrapper gets a graceful stub first, so a wrapper that cannot be imported
# on this platform never breaks the import of this module — the tool simply
# reports that it is unavailable.
def _load_platform_wrapper(module_name: str):
    """Import tools/wrapper_<module_name>.py by path and return the module."""
    import importlib.util

    path = os.path.join(_ROOT_DIR, "tools", f"wrapper_{module_name}.py")
    spec = importlib.util.spec_from_file_location(f"wrapper_{module_name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unavailable_wrapper(tool_name: str):
    """Return a stub for a platform wrapper that could not be imported."""
    def _stub(*_args, **_kwargs):
        return f"[ERROR] '{tool_name}' is not available on this system."
    return _stub


def _load_optional_wrapper(module_name: str):
    """Load a wrapper module, or None when it cannot be imported here."""
    try:
        return _load_platform_wrapper(module_name)
    except Exception as exc:  # pragma: no cover - platform dependent
        print(f"{YELLOW}[WARN] Platform wrapper '{module_name}' unavailable: "
              f"{exc}{RESET}")
        return None


get_contacts = _unavailable_wrapper("get_contacts")
get_contact_by_id = _unavailable_wrapper("get_contact_by_id")
list_sensors = _unavailable_wrapper("list_sensors")
get_sensor_data = _unavailable_wrapper("get_sensor_data")
set_wallpaper = _unavailable_wrapper("set_wallpaper")
take_photo = _unavailable_wrapper("take_photo")
record_video = _unavailable_wrapper("record_video")
record_audio = _unavailable_wrapper("record_audio")
pick_file = _unavailable_wrapper("pick_file")
share_file = _unavailable_wrapper("share_file")
share_text = _unavailable_wrapper("share_text")
toggle_torch = _unavailable_wrapper("toggle_torch")
get_battery_status = _unavailable_wrapper("get_battery_status")
get_wifi_scan_info = _unavailable_wrapper("get_wifi_scan_info")
get_hostname = _unavailable_wrapper("get_hostname")
get_uptime = _unavailable_wrapper("get_uptime")

_contacts_wrapper = _load_optional_wrapper("termux_contacts")
if _contacts_wrapper is not None:
    get_contacts = _contacts_wrapper.get_contacts
    get_contact_by_id = _contacts_wrapper.get_contact_by_id

_sensors_wrapper = _load_optional_wrapper("termux_sensors")
if _sensors_wrapper is not None:
    list_sensors = _sensors_wrapper.list_sensors
    get_sensor_data = _sensors_wrapper.get_sensor_data

_camera_wrapper = _load_optional_wrapper("termux_camera")
if _camera_wrapper is not None:
    take_photo = _camera_wrapper.take_photo
    record_video = _camera_wrapper.record_video

_audio_wrapper = _load_optional_wrapper("termux_audio")
if _audio_wrapper is not None:
    record_audio = _audio_wrapper.record_audio

_filepicker_wrapper = _load_optional_wrapper("termux_filepicker")
if _filepicker_wrapper is not None:
    pick_file = _filepicker_wrapper.pick_file
    share_file = _filepicker_wrapper.share_file
    share_text = _filepicker_wrapper.share_text

_wallpaper_wrapper = _load_optional_wrapper("termux_wallpaper")
if _wallpaper_wrapper is not None:
    set_wallpaper = _wallpaper_wrapper.set_wallpaper

_torch_wrapper = _load_optional_wrapper("termux_torch")
if _torch_wrapper is not None:
    toggle_torch = _torch_wrapper.toggle_torch

_battery_wrapper = _load_optional_wrapper("termux_battery_status")
if _battery_wrapper is not None:
    get_battery_status = _battery_wrapper.get_battery_status

_wifi_wrapper = _load_optional_wrapper("termux_wifi_scaninfo")
if _wifi_wrapper is not None:
    get_wifi_scan_info = _wifi_wrapper.get_wifi_scan_info

_hostname_wrapper = _load_optional_wrapper("system_hostname")
if _hostname_wrapper is not None:
    get_hostname = _hostname_wrapper.get_hostname

_uptime_wrapper = _load_optional_wrapper("system_uptime")
if _uptime_wrapper is not None:
    get_uptime = _uptime_wrapper.get_uptime

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
#: How long the diagnosis collector waits for the individual checks.
#: The device commands behind them are slow — a battery broadcast or a Wi-Fi
#: connection query takes several seconds on a loaded phone — so the budget
#: has to be generous enough to be worth waiting for, and the whole snapshot
#: still has to stay under the 15s the tool contract asks for.
DIAGNOSIS_TIMEOUT = 13
#: Extra grace period after the deadline, so a check that is about to answer
#: still gets into the snapshot instead of being reported as a timeout.
DIAGNOSIS_GRACE = 1


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
            "name": "shell",
            "description": (
                "Execute shell commands inside the system environment (Termux, Linux, or Windows). "
                "Supports an optional execution timeout in seconds. "
                "The timeout applies PER STAGE: every command of a chain "
                "('a && b', 'a; b', 'a | b') gets its own budget, and each stage "
                "is terminated together with the processes it spawns. "
                "Use 'total_timeout' for a chain-wide ceiling that bounds the whole "
                "command regardless of how many stages it has."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "bash": {
                        "type": "string",
                        "description": "Shell command to execute. **You should put the command you want to execute in parameter 'bash'.**",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": (
                            "Maximum execution time in seconds for each command in the chain "
                            "(0 = no limit). A stage that exceeds it is killed along with all "
                            "the processes it spawned."
                        ),
                        "default": 30,
                        "minimum": 0,
                    },
                    "total_timeout": {
                        "type": "integer",
                        "description": (
                            "Maximum execution time in seconds for the WHOLE chain, "
                            "independent of the number of stages (0 = derive it from the "
                            "per-stage timeout). Use this to bound a long chain: the whole "
                            "process tree is killed when the ceiling is reached."
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
            "name": "browse_web",
            "description": (
                "Fetch a URL and convert readable content into structured markdown. "
                "Uses a headless Chromium browser (via Chrome DevTools Protocol) to "
                "properly render JavaScript-heavy pages, single-page applications, and "
                "dynamically loaded content. Supports CSS selectors to target specific "
                "elements. Falls back to requests+BeautifulSoup if Chromium is unavailable. "
                "The browser is closed again when the call returns; use the 'browser' tool "
                "when you need to keep a page alive and interact with it over several calls. "
                "By default a screenshot of the page is captured and attached to the reply "
                "so a multimodal model can look at it. "
                "Never guess click coordinates: add a 'find' action to list the page's "
                "buttons, links and fields — each with the label a human would read and "
                "a ready-to-use CSS selector — and then click by 'text' or 'index'. The "
                "position is resolved in the live DOM, so nothing is guessed."
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
                    "screenshot": {
                        "type": "boolean",
                        "description": (
                            "Capture the page and attach the image to the reply so the "
                            "model can see it. Default true."
                        ),
                        "default": True,
                    },
                    "extract_html": {
                        "type": "boolean",
                        "description": (
                            "Also return the raw outerHTML of the page (or matched "
                            "element(s)) so the model can inspect the DOM structure, "
                            "attributes and data-* values. Default false."
                        ),
                        "default": False,
                    },
                    "actions": {
                        "type": "array",
                        "description": (
                            "Optional list of actions to perform on the page before extraction. "
                            "Each action is a dict with a 'type' field ('click', 'input', 'wait', "
                            "'scroll', 'press', 'select', 'evaluate' or 'find'), a 'selector' "
                            "field (CSS selector for click/input/select), an 'x' field and 'y' "
                            "field (raw viewport coordinates), a 'wait_after' field (seconds to "
                            "wait after the action, default 1.0), a 'text' field (for 'input': "
                            "the text to type; for 'click': the visible label of the element to "
                            "click), a 'label' field (for 'input': the visible label of the field "
                            "to type into), an 'index' field (for 'click': the 1-based index of "
                            "an element from a 'find' listing), a 'query' field (for 'find': only "
                            "list elements matching this word), a 'value' field (option to choose, "
                            "for select), a 'key' field (key name, for press), an 'amount' field "
                            "(pixels to scroll, for scroll) and an 'expression' field (JavaScript "
                            "to evaluate, required for evaluate). Do NOT guess click coordinates: "
                            "run a 'find' action first and then click by 'text' or 'index' — the "
                            "element is located in the live DOM and clicked at its centre. "
                            "Actions are best-effort; the fallback scraper may not support all "
                            "action types."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["click", "input", "wait", "scroll",
                                             "press", "select", "evaluate", "find"],
                                    "description": "The action type to perform.",
                                },
                                "selector": {
                                    "type": "string",
                                    "description": "CSS selector for the target element (required for click, input and select; click can also use x/y coordinates instead).",
                                },
                                "x": {
                                    "type": "number",
                                    "description": (
                                        "Raw viewport X coordinate for a click. Only use it "
                                        "when you mean that exact spot — prefer 'text' or "
                                        "'index', which are resolved against the live DOM."
                                    ),
                                },
                                "y": {
                                    "type": "number",
                                    "description": (
                                        "Raw viewport Y coordinate for a click. Pair it with "
                                        "'x'. Prefer 'text' or 'index' over guessing."
                                    ),
                                },
                                "expression": {
                                    "type": "string",
                                    "description": "JavaScript expression to evaluate in the page context (required for evaluate actions).",
                                },
                                "text": {
                                    "type": "string",
                                    "description": (
                                        "For 'input': the text to type into the element. "
                                        "For 'click': the visible label of the element to "
                                        "click (e.g. 'Submit')."
                                    ),
                                },
                                "label": {
                                    "type": "string",
                                    "description": (
                                        "For 'input': the visible label of the field to type "
                                        "into (e.g. 'Email'). The field is located in the "
                                        "live DOM, so no selector is needed."
                                    ),
                                },
                                "index": {
                                    "type": "integer",
                                    "description": (
                                        "For 'click': the 1-based index of an element from a "
                                        "'find' listing. The element is located in the live "
                                        "DOM at the moment of the click."
                                    ),
                                },
                                "query": {
                                    "type": "string",
                                    "description": (
                                        "For 'find': only list elements whose label or "
                                        "selector contains this word."
                                    ),
                                },
                                "value": {
                                    "type": "string",
                                    "description": "Option value to choose (for select actions).",
                                },
                                "key": {
                                    "type": "string",
                                    "description": "Key to press, e.g. 'Enter', 'Tab', 'Escape', 'ArrowDown' (for press actions).",
                                },
                                "amount": {
                                    "type": "number",
                                    "description": "Pixels to scroll by (for scroll actions), default 600.",
                                },
                                "direction": {
                                    "type": "string",
                                    "description": "Scroll direction: 'up', 'down', 'top' or 'bottom' (for scroll actions).",
                                },
                                "wait_after": {
                                    "type": "number",
                                    "description": "Seconds to wait after the action (default 1.0).",
                                    "default": 1.0,
                                },
                            },
                        },
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser",
            "description": (
                "Drive a persistent browser session that stays alive between calls. "
                "Open a page with action='open' and you get back a short session id "
                "(for example 'b1'); pass that id back with every later call to keep "
                "working on the same page — clicking buttons, typing into fields, "
                "scrolling, pressing keys and choosing options all work on the live "
                "page, with its cookies and form state intact. Use action='close' with "
                "the id to shut the browser down when you are done. "
                "action='list' shows the open sessions. "
                "A screenshot of the page is captured and attached to the reply by "
                "default, so a multimodal model can see what the page looks like. "
                "To click something, do not guess coordinates: run a 'find' action to "
                "list the page's buttons, links and fields with their labels and "
                "ready-to-use CSS selectors, then click by 'text' or 'index' — the "
                "element is located in the live DOM and clicked at its centre."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["open", "act", "navigate", "screenshot", "state",
                                 "close", "list"],
                        "description": (
                            "open: launch a browser and open a URL. "
                            "act: run actions in an open session. "
                            "navigate: load a new URL in an open session. "
                            "screenshot: capture the current viewport. "
                            "state: report the URL, title and page text. "
                            "close: shut the session down. "
                            "list: list the open sessions."
                        ),
                        "default": "open",
                    },
                    "url": {
                        "type": "string",
                        "description": "The URL to open (required for action='open' and action='navigate').",
                    },
                    "browser_id": {
                        "type": "string",
                        "description": (
                            "The session id returned by a previous 'open' call, "
                            "e.g. 'b1'. Required for every action except 'open' and 'list'."
                        ),
                    },
                    "selector": {
                        "type": "string",
                        "description": "Optional CSS selector to filter the extracted text (e.g., 'main', 'article').",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters of text to return. Default 12000, max 50000.",
                        "default": 12000,
                        "minimum": 1000,
                        "maximum": 50000,
                    },
                    "screenshot": {
                        "type": "boolean",
                        "description": (
                            "Capture the page and attach the image to the reply so the "
                            "model can see it. Default true."
                        ),
                        "default": True,
                    },
                    "extract_html": {
                        "type": "boolean",
                        "description": (
                            "Also return the raw outerHTML of the page so the model can "
                            "inspect the DOM structure, attributes and data-* values."
                        ),
                        "default": False,
                    },
                    "actions": {
                        "type": "array",
                        "description": (
                            "Actions to run for action='act'. Each action is a dict with "
                            "a 'type' field ('click', 'input', 'wait', 'scroll', 'press', "
                            "'select', 'evaluate' or 'find'), a 'selector' field (CSS "
                            "selector for click/input/select), an 'x' field and 'y' field "
                            "(raw viewport coordinates), a 'wait_after' field (seconds to "
                            "wait after the action, default 1.0), a 'text' field (for "
                            "'input': the text to type; for 'click': the visible label of "
                            "the element to click), a 'label' field (for 'input': the "
                            "visible label of the field to type into), an 'index' field "
                            "(for 'click': the 1-based index of an element from a 'find' "
                            "listing), a 'query' field (for 'find': only list elements "
                            "matching this word), a 'value' field (option to choose, for "
                            "select), a 'key' field (key to press), an 'amount' field "
                            "(pixels to scroll) and an 'expression' field (JavaScript to "
                            "evaluate, for evaluate actions). Do NOT guess click "
                            "coordinates: run a 'find' action first and then click by "
                            "'text' or 'index' — the element is located in the live DOM "
                            "and clicked at its centre."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["click", "input", "wait", "scroll",
                                             "press", "select", "evaluate", "find"],
                                    "description": "The action type to perform.",
                                },
                                "selector": {
                                    "type": "string",
                                    "description": "CSS selector for the target element (required for click, input and select; click can also use x/y coordinates instead).",
                                },
                                "x": {
                                    "type": "number",
                                    "description": (
                                        "Raw viewport X coordinate for a click. Only use it "
                                        "when you mean that exact spot — prefer 'text' or "
                                        "'index', which are resolved against the live DOM."
                                    ),
                                },
                                "y": {
                                    "type": "number",
                                    "description": (
                                        "Raw viewport Y coordinate for a click. Pair it with "
                                        "'x'. Prefer 'text' or 'index' over guessing."
                                    ),
                                },
                                "expression": {
                                    "type": "string",
                                    "description": "JavaScript expression to evaluate in the page context (required for evaluate actions).",
                                },
                                "text": {
                                    "type": "string",
                                    "description": (
                                        "For 'input': the text to type into the element. "
                                        "For 'click': the visible label of the element to "
                                        "click (e.g. 'Submit')."
                                    ),
                                },
                                "label": {
                                    "type": "string",
                                    "description": (
                                        "For 'input': the visible label of the field to type "
                                        "into (e.g. 'Email'). The field is located in the "
                                        "live DOM, so no selector is needed."
                                    ),
                                },
                                "index": {
                                    "type": "integer",
                                    "description": (
                                        "For 'click': the 1-based index of an element from a "
                                        "'find' listing. The element is located in the live "
                                        "DOM at the moment of the click."
                                    ),
                                },
                                "query": {
                                    "type": "string",
                                    "description": (
                                        "For 'find': only list elements whose label or "
                                        "selector contains this word."
                                    ),
                                },
                                "value": {
                                    "type": "string",
                                    "description": "Option value to choose (for select actions).",
                                },
                                "key": {
                                    "type": "string",
                                    "description": "Key to press, e.g. 'Enter', 'Tab', 'Escape', 'ArrowDown' (for press actions).",
                                },
                                "amount": {
                                    "type": "number",
                                    "description": "Pixels to scroll by (for scroll actions), default 600.",
                                },
                                "direction": {
                                    "type": "string",
                                    "description": "Scroll direction: 'up', 'down', 'top' or 'bottom' (for scroll actions).",
                                },
                                "wait_after": {
                                    "type": "number",
                                    "description": "Seconds to wait after the action (default 1.0).",
                                    "default": 1.0,
                                },
                            },
                        },
                    },
                },
                "required": [],
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
            "description": "React to a specific WhatsApp message with an emoji (e.g. thumbs-up heart smile).",
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
    # ── Gmail (plugin: gmail) ──────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "send_gmail_message",
            "description": (
                "Send an email through the connected Gmail account. "
                "Plain text only — attachments are not supported."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to":      {"type": "string", "description": "Recipient email address (comma-separated for several)."},
                    "subject": {"type": "string", "description": "Subject line of the email."},
                    "body":    {"type": "string", "description": "Body text of the email."},
                    "cc":      {"type": "string", "description": "Optional CC recipients."},
                    "bcc":     {"type": "string", "description": "Optional BCC recipients."},
                },
                "required": ["to", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_gmail_messages",
            "description": (
                "List recent Gmail messages. Supports Gmail search syntax "
                "(e.g. 'from:alice subject:invoice is:unread')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query":      {"type": "string", "description": "Gmail search query. Empty = newest messages.", "default": ""},
                    "max_results": {"type": "integer", "description": "How many messages to return (1-100, default 10).", "default": 10},
                    "label_ids":  {"type": "string", "description": "Comma-separated label ids to restrict to (e.g. INBOX, UNREAD).", "default": ""},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_gmail",
            "description": (
                "Search Gmail and return matching messages with their snippets. "
                "Use Gmail search syntax (e.g. 'from:bob has:attachment newer_than:7d')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query":       {"type": "string", "description": "Gmail search query."},
                    "max_results": {"type": "integer", "description": "How many matches to return (1-100, default 20).", "default": 20},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_gmail_labels",
            "description": "List the Gmail labels (folders) of the connected account.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_gmail_label",
            "description": "Create a new Gmail label (folder).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name of the new label."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_gmail_message",
            "description": (
                "Delete a Gmail message — by default it moves to the trash, "
                "with permanent=true it is deleted for good."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "The Gmail message id."},
                    "permanent":  {"type": "boolean", "description": "Delete permanently instead of trashing. Default false.", "default": False},
                },
                "required": ["message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_gmail_message",
            "description": (
                "Read the full content of a single Gmail message by ID "
                "(headers + body + attachments). Use this after listing or "
                "searching Gmail to actually read a message's content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "The Gmail message id to read."},
                    "format":     {"type": "string", "description": "'full' (headers + body) or 'metadata' (headers + snippet only). Default 'full'.", "default": "full"},
                },
                "required": ["message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_gmail_profile",
            "description": (
                "Show the connected Gmail account's profile: the Gmail "
                "address, total message/thread counts, and history id."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    # ── Google Calendar (plugin: google_calendar) ────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_calendar_events",
            "description": (
                "List Google Calendar events of a calendar ('primary' by default). "
                "Times are ISO-8601; leave them empty to list everything."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "calendar_id": {"type": "string", "description": "Calendar id. 'primary' is the user's own calendar.", "default": "primary"},
                    "time_min":    {"type": "string", "description": "Only events starting after this ISO-8601 time.", "default": ""},
                    "time_max":    {"type": "string", "description": "Only events starting before this ISO-8601 time.", "default": ""},
                    "max_results": {"type": "integer", "description": "How many events to return (1-250, default 25).", "default": 25},
                    "query":       {"type": "string", "description": "Free-text filter on the event title.", "default": ""},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": (
                "Create a Google Calendar event. 'start' and 'end' are ISO-8601 "
                "times (e.g. 2026-03-04T15:00:00); a naive time is read as local time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "calendar_id": {"type": "string", "description": "Calendar id. 'primary' by default.", "default": "primary"},
                    "summary":     {"type": "string", "description": "Title of the event."},
                    "start":       {"type": "string", "description": "Start time (ISO-8601)."},
                    "end":         {"type": "string", "description": "End time (ISO-8601)."},
                    "description": {"type": "string", "description": "Event description.", "default": ""},
                    "location":    {"type": "string", "description": "Where the event happens.", "default": ""},
                    "attendees":   {"type": "array", "items": {"type": "string"}, "description": "Email addresses of the guests.", "default": []},
                },
                "required": ["summary", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_calendar_event",
            "description": (
                "Update a Google Calendar event. Only the fields you pass are changed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id":    {"type": "string", "description": "The event id to update."},
                    "calendar_id": {"type": "string", "description": "Calendar id. 'primary' by default.", "default": "primary"},
                    "summary":     {"type": "string", "description": "New title.", "default": ""},
                    "start":       {"type": "string", "description": "New start time (ISO-8601).", "default": ""},
                    "end":         {"type": "string", "description": "New end time (ISO-8601).", "default": ""},
                    "description": {"type": "string", "description": "New description.", "default": ""},
                    "location":    {"type": "string", "description": "New location.", "default": ""},
                    "attendees":   {"type": "array", "items": {"type": "string"}, "description": "New guest email addresses.", "default": []},
                },
                "required": ["event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": "Delete a Google Calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id":    {"type": "string", "description": "The event id to delete."},
                    "calendar_id": {"type": "string", "description": "Calendar id. 'primary' by default.", "default": "primary"},
                },
                "required": ["event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_calendars",
            "description": "List the Google Calendars the connected account can access.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    # ── Google Drive (plugin: google_drive) ──────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_drive_files",
            "description": (
                "List Google Drive files. 'query' uses Drive's query language, "
                "e.g. \"name contains 'report' and trashed = false\"."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query":     {"type": "string", "description": "Drive query string. Empty = all files.", "default": ""},
                    "page_size": {"type": "integer", "description": "How many files to return (1-1000, default 25).", "default": 25},
                    "order_by":  {"type": "string", "description": "Sort order, e.g. 'modifiedTime desc' or 'name'.", "default": "modifiedTime desc"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upload_drive_file",
            "description": "Upload a local file to Google Drive.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path":       {"type": "string", "description": "Path of the local file to upload."},
                    "name":            {"type": "string", "description": "Name to store it under. Defaults to the file name.", "default": ""},
                    "parent_folder_id": {"type": "string", "description": "Id of the Drive folder to upload into. Empty = My Drive.", "default": ""},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_drive_file",
            "description": (
                "Download a Google Drive file to the device. "
                "Without 'output_path' the file lands in workspace/downloads/."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":    {"type": "string", "description": "The Drive file id."},
                    "output_path": {"type": "string", "description": "Where to save the file.", "default": ""},
                },
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_drive_file",
            "description": "Delete a Google Drive file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "The Drive file id."},
                },
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_drive_folder",
            "description": "Create a folder in Google Drive.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":             {"type": "string", "description": "Name of the folder."},
                    "parent_folder_id": {"type": "string", "description": "Id of the parent folder. Empty = My Drive.", "default": ""},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "share_drive_file",
            "description": "Share a Google Drive file with somebody by email.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id":         {"type": "string", "description": "The Drive file id."},
                    "email":           {"type": "string", "description": "Who to share it with."},
                    "role":            {"type": "string", "enum": ["reader", "commenter", "writer"], "description": "Access level. Default 'reader'.", "default": "reader"},
                    "permission_type": {"type": "string", "enum": ["user", "anyone"], "description": "Share with a person or with anyone who has the link. Default 'user'.", "default": "user"},
                },
                "required": ["file_id", "email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_plugin",
            "description": (
                "Inspect and control Termux-AI plugins. Use this whenever the user "
                "asks to turn a plugin on or off, asks whether a plugin is working, "
                "or wants to connect Gmail, Google Calendar or Google Drive. "
                "Actions: 'list' shows every plugin with its state, 'status' covers "
                "one plugin, 'enable' turns a plugin on, 'disable' turns it off. "
                "Enabling a plugin also starts whatever it needs to run (the "
                "Termux-WP node gateway for WhatsApp) and puts its tools into my "
                "tool context right away — so after 'enable' I can use them in the "
                "same turn instead of asking the user to repeat the request. "
                "For WhatsApp, 'setup' is the pairing step: the session is paired "
                "by scanning a code, and that code is only ever drawn on the "
                "user's screen when they ask for it — it is never printed on its "
                "own. If the session is already linked, 'setup' stops the gateway "
                "and switches the plugin off instead (a pairing code is only "
                "wanted for a re-link), so say that before calling it. "
                "Enabling an account plugin (gmail, google_calendar, google_drive) "
                "is not enough on its own — the Google account must grant access "
                "first. 'setup' (or 'enable') starts that permission request and "
                "returns a URL the user has to open; after they confirm they "
                "approved it, call the tool again with 'finish' to complete the "
                "connection. 'disconnect' revokes the stored Google access. "
                "If the user approves on another device, pass the copied redirect "
                "URL as 'code' with action 'finish'. The Google OAuth client must "
                "be of application type 'Desktop app'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "status", "enable", "disable", "setup",
                                 "finish", "disconnect"],
                        "description": "What to do. Default 'list'.",
                        "default": "list",
                    },
                    "plugin": {
                        "type": "string",
                        "description": (
                            "Plugin name: 'gmail', 'google_calendar', "
                            "'google_drive' or 'whatsapp'. Required for every "
                            "action except 'list'."
                        ),
                    },
                    "code": {
                        "type": "string",
                        "description": (
                            "Optional: the redirect URL (or authorization code) "
                            "the user copied from the browser after approving the "
                            "request on another device. Used with action 'finish'."
                        ),
                    },
                },
                "required": ["action"],
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
            "name": "retrieve_archive",
            "description": (
                "Retrieve the raw content of a compressed context segment by its "
                "archive ID (e.g. 'A3'). When the context outgrew the model's "
                "window, older reasoning details, tool calls and tool outputs "
                "were replaced by a digest; this reads the original text back "
                "so the work can continue from where it stopped. The content "
                "is paged: pass offset/limit to read the next slice."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "archive_id": {
                        "type": "string",
                        "description": "The archive ID shown in the [Compressed context ...] marker, e.g. 'A3'."
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Character offset to start reading from. Default 0.",
                        "default": 0
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum characters to return. Default 20000.",
                        "default": 20000
                    }
                },
                "required": ["archive_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_archives",
            "description": (
                "List compressed context segments archived in this session, with "
                "their IDs, sizes and one-line descriptions."
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
                "Delegate a subtask to a sub-AI and receive its structured report back. "
                "Pick the sub-AI by INTELLIGENCE LEVEL, not by model id: 'high' routes to "
                "the largest reasoning-capable FREE models (Gemini 3.8 Flash, MiniMax "
                "M3.1 Flash, Qwen3.8-27B, Nemotron 3.5 Super VL, Nemotron 3 Ultra, "
                "Mistral Large 4), 'medium' to a step down "
                "(GPT-OSS 120B, Poolside Laguna XS, Nemotron 3 Super, Gemma 4 31B), and "
                "'low' to the cheap and fast end (Gemini 2.5 Flash, Gemini Flash-Lite, Gemma 4, "
                "Cohere North Mini Code). Every route is free — no paid model is ever "
                "used — and only tool-calling capable models are in the pool. "
                "The sub-AI can use tools provided by the parent agent to gather information "
                "and perform actions during task execution."
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
                    "intelligence": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": (
                            "Intelligence level of the sub-AI that runs the task. "
                            "high = largest reasoning models for hard, multi-step work; "
                            "medium = balanced models for normal engineering tasks; "
                            "low = fastest/cheapest models for lookups, summaries and mechanical edits."
                        ),
                        "default": "medium",
                    },
                    "available_tools": {
                        "type": "array",
                        "description": (
                            "List of tool names the sub-AI is allowed to use. "
                            "If omitted, the sub-AI gets the core delegation toolkit "
                            "(shell, read_file, write_file, find_replace, list_directory, "
                            "search_files, search_in_files, index_files, browse_web, "
                            "http_request, get_datetime). Pass an explicit list to widen "
                            "or narrow it."
                        ),
                        "items": {"type": "string"},
                    },
                    "tool_call_iterations": {
                        "type": "integer",
                        "description": "Maximum number of tool-calling rounds the sub-AI may perform (default 5). Set to 0 to disable tool access entirely.",
                        "default": 5,
                        "minimum": 0,
                        "maximum": 20,
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "Optional override for the system prompt sent to the sub-AI model. If omitted, a default delegation-focused system prompt is used.",
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
            "description": (
                "Generate an image from a text prompt. "
                "Provider 'google' uses Gemini native image generation "
                "(Nano Banana); provider 'nvidia' uses NVIDIA's free FLUX "
                "endpoints; provider 'auto' tries Google first and falls back "
                "to NVIDIA. The finished picture is attached to the reply so "
                "a multimodal model can look at it. "
                "NVIDIA models available on the hosted free API: "
                "black-forest-labs/flux-3-image-klein, "
                "black-forest-labs/flux.2-klein-4b, "
                "black-forest-labs/flux.1-dev, "
                "black-forest-labs/flux.1-schnell and "
                "black-forest-labs/flux.1-kontext-dev (the last one edits an "
                "input image). "
                "NVIDIA also lists qwen/qwen-image, qwen/qwen-image-edit, "
                "nvidia/qwen-image-edit-nvpcb-ovsl2sl and "
                "stabilityai/stable-diffusion-3.5-large, but those are "
                "self-hosted NIMs and are not served from the hosted API."
            ),
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
                        "description": (
                            "Quality tier. 'flash' is the fast distilled model, "
                            "'pro' the highest-fidelity one. Default 'flash'."
                        ),
                        "default": "flash",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Base filename without extension.",
                    },
                    "provider": {
                        "type": "string",
                        "enum": ["auto", "google", "nvidia"],
                        "description": (
                            "Which image backend to use. "
                            "'auto' tries Google then NVIDIA. Default 'auto'."
                        ),
                        "default": "auto",
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Pin an exact image model id, e.g. "
                            "'gemini-3.1-flash-image-preview' or "
                            "'black-forest-labs/flux.2-klein-4b'. "
                            "Overrides provider/quality selection."
                        ),
                    },
                    "width": {
                        "type": "integer",
                        "description": (
                            "Output width in pixels. Every NVIDIA endpoint "
                            "accepts only a fixed set of literals, so the "
                            "value is snapped onto the model's grid "
                            "(flux-3 / flux.2-klein: 512-1568 in steps of 16; "
                            "flux.1-dev / schnell: 768-1344 in steps of 64; "
                            "kontext: seventeen literals from 672 to 1568) "
                            "and kept under the 1062400 pixel budget."
                        ),
                        "minimum": 64,
                        "maximum": 4096,
                    },
                    "height": {
                        "type": "integer",
                        "description": (
                            "Output height in pixels, snapped the same way as "
                            "the width."
                        ),
                        "minimum": 64,
                        "maximum": 4096,
                    },
                    "seed": {
                        "type": "integer",
                        "description": (
                            "Random seed for reproducible output. "
                            "0 (default) means random."
                        ),
                        "minimum": 0,
                    },
                    "steps": {
                        "type": "integer",
                        "description": (
                            "Diffusion steps. NVIDIA models accept 1-50 "
                            "(flux.2-klein-4b and flux.1-schnell: 1-4, "
                            "flux.1-dev and kontext: 5-100); out-of-range "
                            "values are clamped."
                        ),
                        "minimum": 1,
                        "maximum": 100,
                    },
                    "cfg_scale": {
                        "type": "number",
                        "description": (
                            "Prompt adherence. NVIDIA models accept 1-20 "
                            "(flux.2-klein-4b is capped at 1, flux.1-schnell "
                            "at 1, flux.1-dev and kontext 1-9); out-of-range "
                            "values are clamped."
                        ),
                        "minimum": 0,
                        "maximum": 20,
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": (
                            "What the image should avoid. Only the models "
                            "that support it (flux-3-image-klein) use it; the "
                            "others ignore it rather than failing."
                        ),
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
    # ── Termux API wrappers (advertised to LLM so it knows these exist) ──
    {
        "type": "function",
        "function": {
            "name": "get_contacts",
            "description": "Retrieve all contacts on the device as a list of dicts.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_contact_by_id",
            "description": "Retrieve a specific contact by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_id": {"type": "string", "description": "The contact ID to look up."},
                },
                "required": ["contact_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_sensors",
            "description": "List all available device sensors (accelerometer, gyroscope, etc.).",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sensor_data",
            "description": "Get sensor data for a specific sensor type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sensor_type": {"type": "string", "description": "Type of sensor (accelerometer, gyroscope, magnetometer, light, proximity, pressure, temperature, humidity, step_counter, step_detector)."},
                    "samples":     {"type": "integer", "description": "Number of samples to collect.", "default": 1},
                    "delay":       {"type": "integer", "description": "Delay between samples in milliseconds.", "default": 100},
                },
                "required": ["sensor_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_wallpaper",
            "description": "Set the device wallpaper from an image file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "Path to the image file to set as wallpaper."},
                    "lockscreen": {"type": "boolean", "description": "If true, set lockscreen wallpaper instead of home screen.", "default": False},
                },
                "required": ["image_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_photo",
            "description": "Take a photo using the device camera.",
            "parameters": {
                "type": "object",
                "properties": {
                    "output_path": {"type": "string", "description": "Path to save the photo (auto-generated if not provided).", "default": None},
                    "camera_id":   {"type": "integer", "description": "Camera ID (0 = back, 1 = front).", "default": 0},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_video",
            "description": "Record a video using the device camera.",
            "parameters": {
                "type": "object",
                "properties": {
                    "output_path": {"type": "string", "description": "Path to save the video (auto-generated if not provided).", "default": None},
                    "camera_id":   {"type": "integer", "description": "Camera ID (0 = back, 1 = front).", "default": 0},
                    "limit":       {"type": "integer", "description": "Maximum duration in seconds (0 = no limit).", "default": 0},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_audio",
            "description": "Record audio for a specific duration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "output_path": {"type": "string", "description": "Path to save the recording (auto-generated if not provided).", "default": None},
                    "format":      {"type": "string", "description": "Audio format (aac, amr, 3gp, etc.).", "default": "aac"},
                    "bitrate":     {"type": "integer", "description": "Bitrate in bits per second.", "default": 128000},
                    "duration":    {"type": "integer", "description": "Recording duration in seconds.", "default": 10},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pick_file",
            "description": "Open a file picker to select file(s).",
            "parameters": {
                "type": "object",
                "properties": {
                    "multiple":   {"type": "boolean", "description": "Allow multiple file selection.", "default": False},
                    "mime_type":  {"type": "string", "description": "MIME type filter (e.g., 'image/*', 'application/pdf').", "default": "*/*"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "share_file",
            "description": "Share a file using the Android share sheet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to the file to share."},
                    "mime_type": {"type": "string", "description": "MIME type of the file.", "default": None},
                    "subject":   {"type": "string", "description": "Subject line for the share.", "default": None},
                    "text":      {"type": "string", "description": "Additional text to share.", "default": None},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "share_text",
            "description": "Share text using the Android share sheet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text":    {"type": "string", "description": "The text to share."},
                    "subject": {"type": "string", "description": "Subject line for the share.", "default": None},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_torch",
            "description": "Toggle the device LED torch (flashlight) on or off.",
            "parameters": {
                "type": "object",
                "properties": {
                    "on": {
                        "type": "boolean",
                        "description": "True to turn on, False to turn off.",
                        "default": True
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_uptime",
            "description": (
                "Retrieve system uptime and idle time. "
                "Runs the `uptime` command on Linux/macOS and a PowerShell equivalent on Windows. "
                "Returns a dict with 'uptime', 'idle_time', and 'raw' keys."
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
            "name": "get_hostname",
            "description": (
                "Retrieve the system hostname and fully qualified domain name (FQDN). "
                "Uses socket.gethostname() and platform.node(). "
                "Returns a dict with 'hostname' and 'fqdn' keys."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]

def log_write(msg: str) -> None:
    """Append a line to the live session log (``logs/log.txt``).

    Every tool that wants to record what it is about to do calls this.  The
    write is flushed immediately, so the log shows the call while it is still
    running — not after the turn has finished.  That is what makes the log
    useful for recovering a session that was cut short: the last line tells
    you what the runtime was doing when it died.
    """
    runtime_log.log_line(f"[{runtime_log.stamp()}] {msg}")


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
    from core import permissions

    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(paths.ROOT, p)

    # Permission check - api.keys is always gated; protected/outside-root
    # paths require permission.  Nothing is auto-rejected: the user is asked.
    approved, reason = permissions.request_file_permission("read", p)
    if not approved:
        return reason

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

    # Permission check - api.keys is always gated; protected/outside-root
    # paths require permission.  Nothing is auto-rejected: the user is asked.
    approved, reason = permissions.request_file_permission("write", p)
    if not approved:
        return reason

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

    # Permission check - api.keys is always gated; protected/outside-root
    # paths require permission.  Nothing is auto-rejected: the user is asked.
    approved, reason = permissions.request_file_permission("write", p)
    if not approved:
        return reason

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
                # Never index the api.keys secrets file - it is gated in every mode.
                # api.keys.template is allowed.
                if os.path.basename(filepath) == "api.keys" and not filepath.endswith("api.keys.template"):
                    continue
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    rel_p = os.path.relpath(filepath, paths.ROOT)
                    out.write(f"--- FILE: {rel_p} ---\n{content}\n\n")
                    indexed_count += 1
            except Exception:
                pass

    return f"Successfully indexed {indexed_count} file(s) into indexed_memory.txt."


# ── CHROME PATH & PORT HELPERS ────────────────────────────────────────────────────

def _find_chrome() -> str | None:
    """Locate a Chrome/Chromium executable on the system."""
    candidates = [
        os.environ.get('CHROME_PATH', ''),
        '/data/data/com.termux/files/usr/lib/chromium/chrome',
        '/data/data/com.termux/files/usr/bin/chromium-browser',
        '/usr/bin/chromium-browser',
        '/usr/bin/chromium',
        '/usr/bin/google-chrome',
        '/usr/bin/google-chrome-stable',
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    for name in ('chromium-browser', 'chromium', 'google-chrome', 'google-chrome-stable'):
        try:
            result = subprocess.run(['which', name], capture_output=True, text=True, timeout=3)
            if result.returncode == 0:
                path = result.stdout.strip()
                if path and os.path.isfile(path):
                    return path
        except Exception:
            pass
    return None


def _find_free_port() -> int | None:
    """Find a free TCP port on 127.0.0.1."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    except Exception:
        return None
    finally:
        s.close()


def _wait_for_port(port: int, timeout: float = 15.0) -> bool:
    """Wait until a TCP connection to 127.0.0.1:port succeeds."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect(('127.0.0.1', port))
            s.close()
            return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(0.3)
    return False


# ── CDP (Chrome DevTools Protocol) WEBSOCKET HELPERS ──────────────────────────────

def _send_cdp(ws, method: str, params: dict | None = None, msg_id: int = 1, timeout: float = 10) -> dict:
    """
    Send a Chrome DevTools Protocol command over a WebSocket connection
    and return the matching response.

    CDP sends both command responses (with matching 'id') and event
    notifications (no 'id'). We read until we get the response we want.
    """
    msg: dict = {"id": msg_id, "method": method}
    if params:
        msg["params"] = params
    ws.send(json.dumps(msg))

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = ws.recv()
        except Exception:
            time.sleep(0.05)
            continue
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and "id" in data and data["id"] == msg_id:
            return data
    return {"error": {"message": "timeout waiting for CDP response"}}


def _wait_for_load(ws, timeout: float = 12.0, msg_id: int = 4) -> tuple:
    """
    Poll document.readyState until 'complete' or timeout.

    When the page is *already* 'complete' (typical after a click action where
    navigation hasn't been detected yet), the function briefly waits and re-checks
    so that a navigation triggered by the click is properly caught.

    Returns ``(loaded, next_msg_id)``.  Every poll gets its own DevTools
    message id: the protocol matches a response to its command by id, so
    reusing one would let a command pick up somebody else's answer.
    """
    deadline = time.monotonic() + timeout
    was_complete = False
    recheck_deadline = time.monotonic() + 2.0  # give a navigation 2s to start
    while time.monotonic() < deadline:
        resp = _send_cdp(ws, "Runtime.evaluate", {
            "expression": "document.readyState",
            "returnByValue": True
        }, msg_id)
        msg_id += 1
        result = resp.get("result", {}).get("result", {})
        val = result.get("value", "loading")
        if val == "complete":
            if was_complete:
                # Stayed complete throughout — page is stable
                return True, msg_id
            # First time seeing complete. Give a brief window for navigation
            # to start (click may have triggered a delayed navigation).
            was_complete = True
            if time.monotonic() > recheck_deadline:
                return True, msg_id
            time.sleep(0.2)
            continue
        else:
            was_complete = False
            recheck_deadline = time.monotonic() + 2.0
        time.sleep(0.3)
    return False, msg_id


def _evaluate_js(ws, js: str, msg_id: int = 6, timeout: float = 10) -> tuple:
    """Evaluate JavaScript in the page context and return (value, error)."""
    resp = _send_cdp(ws, "Runtime.evaluate", {
        "expression": js,
        "returnByValue": True,
        "awaitPromise": True
    }, msg_id, timeout=timeout)

    if "error" in resp:
        return None, resp["error"].get("message", "CDP error")

    result_wrapper = resp.get("result", {})
    result = result_wrapper.get("result", {})

    if result_wrapper.get("exceptionDetails"):
        exc = result_wrapper.get("exceptionDetails", {})
        # "Uncaught" on its own tells nobody anything -- the real reason
        # (SyntaxError, ReferenceError, ...) sits in the exception's
        # description.
        detail = ""
        exception = exc.get("exception") or {}
        if isinstance(exception, dict):
            detail = exception.get("description") or exception.get("value") or ""
        message = detail or exc.get("text", "unknown")
        return None, f"JS exception: {message}"

    if result.get("type") == "string":
        return result.get("value", ""), None
    elif result.get("type") == "object" and "value" in result:
        return json.dumps(result["value"], ensure_ascii=False), None
    return result.get("value", ""), None



def _locate_element(ws, selector: str, msg_id: int) -> dict:
    """
    Find an element in the live page and report where it actually is.

    The element is scrolled into view *first*.  An element below the fold has
    a rectangle that lies outside the viewport, and a click aimed at it lands
    on nothing at all while still looking like it worked -- the tool would
    report a click that never reached the button.

    The rectangle is in CSS pixels relative to the viewport, which is exactly
    the coordinate space the DevTools mouse events use, so no scroll offset
    and no device-pixel scaling has to be applied to it.

    Returns a dict:
      found       -- the element exists on the page
      hidden      -- it exists but is not rendered (display:none, visibility:hidden)
      x, y        -- top-left corner of the element
      width, height
      vwidth, vheight -- the viewport size, or None if the page did not say
    """
    # json.dumps gives a properly escaped JS string literal -- a selector
    # containing a quote or a backslash would otherwise break the script.
    js = (
        "(function() {"
        "var el = document.querySelector(" + json.dumps(selector) + ");"
        "if (!el) return {found: false};"
        "var cs = null;"
        "try { cs = window.getComputedStyle(el); } catch (e) {}"
        "if (cs && (cs.display === 'none' || cs.visibility === 'hidden'"
        " || cs.visibility === 'collapse')) {"
        "  return {found: true, hidden: true};"
        "}"
        "try { el.scrollIntoView({block: 'center', inline: 'center',"
        " behavior: 'instant'}); }"
        "catch (e) { try { el.scrollIntoView(); } catch (e2) {} }"
        "var rect = el.getBoundingClientRect();"
        "if (!rect) return {found: true, hidden: true};"
        "if (!rect.width && !rect.height) {"
        "  return {found: true, hidden: true, x: rect.x, y: rect.y,"
        "          width: rect.width, height: rect.height};"
        "}"
        "return {found: true, hidden: false, x: rect.x, y: rect.y,"
        "        width: rect.width, height: rect.height,"
        "        vwidth: window.innerWidth, vheight: window.innerHeight};"
        "})()"
    )
    result, err = _evaluate_js(ws, js, msg_id=msg_id)
    if err or result is None:
        return {"found": False}
    try:
        if isinstance(result, str):
            result = json.loads(result)
        # A page that has no such element answers with a null, which parses
        # into None -- calling .get() on that would raise AttributeError and
        # take the whole tool call down with it.
        if not isinstance(result, dict):
            return {"found": False}
    except (ValueError, TypeError, json.JSONDecodeError):
        return {"found": False}

    # A payload without an explicit 'found' flag but carrying a rectangle
    # still means the element was located.
    if "found" in result:
        found = bool(result.get("found"))
    else:
        found = (result.get("x") is not None
                 or result.get("width") is not None)
    info = {"found": found, "hidden": bool(result.get("hidden"))}
    for key in ("x", "y", "width", "height", "vwidth", "vheight"):
        value = result.get(key)
        if value is None:
            info[key] = None
            continue
        try:
            info[key] = float(value)
        except (ValueError, TypeError):
            info[key] = None
    return info


def _get_element_bounds(ws, selector: str, msg_id: int) -> tuple:
    """
    Get the bounding box of an element via CDP.
    Returns (x, y, width, height) or (None, None, None, None) if not found
    or not visible.
    """
    info = _locate_element(ws, selector, msg_id)
    if not info.get("found") or info.get("hidden"):
        return (None, None, None, None)
    return (info.get("x") or 0.0, info.get("y") or 0.0,
            info.get("width") or 0.0, info.get("height") or 0.0)



# ── ELEMENT DISCOVERY ─────────────────────────────────────────────────────────
#
# A model that can only click by coordinates has to guess where the button
# is.  A model that can click by CSS selector has to guess the *selector*
# instead -- and a wrong guess costs a tool round every single time.  The
# page itself knows where its buttons are and what they are called, so the
# page is asked.
#
# ``_enumerate_interactive()`` returns every visible clickable thing on the
# page together with the label a human would read ("Submit"), a CSS
# selector that is unique on that page, and the element's live rectangle.
# A click can then be aimed by name or by the index from the listing, and
# it is resolved against the live DOM at the moment the click happens --
# no pixel guessing, no stale coordinates.

_INTERACTIVE_SELECTOR = (
    "button, a[href], input, textarea, select, [role=button], [role=link], "
    "[role=checkbox], [role=radio], [role=tab], [role=menuitem], "
    "[onclick], [contenteditable]"
)

_JS_ENUMERATE_INTERACTIVE = r"""
(function () {
  function q(v) {
    return '"' + String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
  }
  function escIdent(v) {
    try { if (window.CSS && CSS.escape) { return CSS.escape(v); } } catch (e) {}
    return String(v).replace(/([^a-zA-Z0-9_-])/g, '\\$1');
  }
  function isUnique(sel) {
    try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; }
  }
  function attrSel(tag, attr, value) {
    return tag + '[' + attr + '=' + q(value) + ']';
  }
  function pathFor(el) {
    var parts = [];
    var node = el;
    var depth = 0;
    while (node && node.nodeType === 1 && depth < 12) {
      var tag = node.tagName.toLowerCase();
      var seg = tag;
      var parent = node.parentElement;
      if (parent) {
        var same = 0, pos = 0;
        for (var i = 0; i < parent.children.length; i++) {
          if (parent.children[i].tagName === node.tagName) {
            same++;
            if (parent.children[i] === node) { pos = same; }
          }
        }
        if (same > 1) { seg += ':nth-of-type(' + pos + ')'; }
      }
      parts.unshift(seg);
      node = parent;
      depth++;
    }
    return parts.join(' > ');
  }
  function selectorFor(el) {
    var tag = el.tagName.toLowerCase();
    var tries = [];
    if (el.id) { tries.push('#' + escIdent(el.id)); }
    var testid = el.getAttribute('data-testid') || el.getAttribute('data-test-id')
              || el.getAttribute('data-cy') || el.getAttribute('data-qa');
    if (testid) { tries.push(attrSel(tag, 'data-testid', testid)); }
    var nm = el.getAttribute('name');
    if (nm) { tries.push(attrSel(tag, 'name', nm)); }
    var aria = el.getAttribute('aria-label');
    if (aria) { tries.push(attrSel(tag, 'aria-label', aria)); }
    tries.push(pathFor(el));
    for (var i = 0; i < tries.length; i++) {
      if (isUnique(tries[i])) { return tries[i]; }
    }
    return tries[tries.length - 1];
  }
  function labelFor(el) {
    var parts = [];
    var aria = el.getAttribute('aria-label');
    if (aria) { parts.push(aria); }
    var labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      try {
        var ref = document.getElementById(labelledby);
        if (ref) { parts.push((ref.innerText || ref.textContent || '').trim()); }
      } catch (e) {}
    }
    if (!parts.length && el.tagName === 'INPUT') {
      var fid = el.getAttribute('id');
      if (fid) {
        try {
          var lab = document.querySelector('label[for=' + q(fid) + ']');
          if (lab) { parts.push((lab.innerText || lab.textContent || '').trim()); }
        } catch (e) {}
      }
    }
    if (!parts.length && el.closest) {
      var own = el.closest('label');
      if (own) { parts.push((own.innerText || own.textContent || '').trim()); }
    }
    if (!parts.length) {
      var text = (el.innerText || el.textContent || '').trim();
      if (text) { parts.push(text); }
    }
    if (!parts.length) {
      var val = el.getAttribute('value');
      if (val) { parts.push(val); }
    }
    if (!parts.length) {
      var ph = el.getAttribute('placeholder');
      if (ph) { parts.push(ph); }
    }
    if (!parts.length) {
      var title = el.getAttribute('title');
      if (title) { parts.push(title); }
    }
    if (!parts.length) {
      var alt = el.getAttribute('alt');
      if (alt) { parts.push(alt); }
    }
    var label = parts.join(' ').replace(/\s+/g, ' ').trim();
    if (label.length > 120) { label = label.slice(0, 117) + '...'; }
    return label;
  }
  function isVisible(el) {
    var cs = null;
    try { cs = window.getComputedStyle(el); } catch (e) {}
    if (cs) {
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.visibility === 'collapse') { return false; }
      if (parseFloat(cs.opacity) === 0) { return false; }
    }
    var rect = el.getBoundingClientRect();
    if (!rect || (!rect.width && !rect.height)) { return false; }
    return true;
  }
  var out = [];
  var nodes = document.querySelectorAll(__SELECTOR__);
  for (var i = 0; i < nodes.length; i++) {
    var el = nodes[i];
    if (!isVisible(el)) { continue; }
    var rect = el.getBoundingClientRect();
    out.push({
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      role: el.getAttribute('role') || '',
      name: labelFor(el),
      selector: selectorFor(el),
      x: Math.round(rect.x),
      y: Math.round(rect.y),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
      cx: Math.round(rect.x + rect.width / 2),
      cy: Math.round(rect.y + rect.height / 2),
      in_viewport: (rect.bottom > 0 && rect.right > 0
                    && rect.top < (window.innerHeight || 0)
                    && rect.left < (window.innerWidth || 0))
    });
  }
  return {elements: out, vwidth: window.innerWidth, vheight: window.innerHeight};
})()
"""


def _enumerate_interactive(ws, msg_id: int) -> tuple[list[dict], int]:
    """
    Ask the live page which things on it can be clicked.

    Returns ``(elements, next_msg_id)``.  Every element carries the label a
    human would read, a CSS selector that is unique on this page, and its
    live viewport rectangle.  Hidden elements are left out -- there is no
    point offering a button nobody can see.
    """
    js = _JS_ENUMERATE_INTERACTIVE.replace(
        "__SELECTOR__", json.dumps(_INTERACTIVE_SELECTOR))
    result, err = _evaluate_js(ws, js, msg_id=msg_id)
    if err or result is None:
        return [], msg_id + 1
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            return [], msg_id + 1
    if not isinstance(result, dict):
        return [], msg_id + 1

    raw = result.get("elements")
    elements: list[dict] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            selector = str(item.get("selector") or "").strip()
            if not selector:
                continue
            elements.append({
                "tag": str(item.get("tag") or ""),
                "type": str(item.get("type") or ""),
                "role": str(item.get("role") or ""),
                "name": str(item.get("name") or ""),
                "selector": selector,
                "x": item.get("x"),
                "y": item.get("y"),
                "width": item.get("width"),
                "height": item.get("height"),
                "cx": item.get("cx"),
                "cy": item.get("cy"),
                "in_viewport": bool(item.get("in_viewport")),
            })
    return elements, msg_id + 1


def _format_elements(elements: list[dict], query: str = "") -> str:
    """Render the element listing the model acts on."""
    if not elements:
        if query:
            return ("no interactive element matches " + repr(query)
                    + " — try a shorter word, or a 'find' without a query")
        return ("no interactive elements found on this page "
                "(no visible buttons, links or form fields)")

    lines = ["--- interactive elements (%d) ---" % len(elements)]
    for i, el in enumerate(elements, 1):
        label = el.get("name") or el.get("type") or el.get("role") or "(no label)"
        bits = ["  [%d]" % i, el.get("tag") or "?"]
        if el.get("type"):
            bits.append("type=" + str(el["type"]))
        if el.get("role"):
            bits.append("role=" + str(el["role"]))
        bits.append('"%s"' % str(label))
        bits.append("selector=" + str(el.get("selector") or ""))
        bits.append("at (%s, %s)" % (el.get("cx"), el.get("cy")))
        if not el.get("in_viewport"):
            bits.append("(outside the viewport — it is scrolled to on click)")
        lines.append(" ".join(bits))
    lines.append('Click one with {"type": "click", "index": N} '
                 'or {"type": "click", "text": "<label>"} '
                 'or {"type": "click", "selector": "<selector>"}.')
    return "\n".join(lines)


def _candidate_hint(elements: list[dict], limit: int = 8) -> str:
    """One line naming what is actually on the page, for a failed lookup."""
    if not elements:
        return "no interactive elements were found on the page"
    parts = []
    for i, el in enumerate(elements[:limit], 1):
        label = el.get("name") or el.get("type") or el.get("role") or "(no label)"
        parts.append('[%d] %s "%s" (%s)' % (i, el.get("tag") or "?", label,
                                            el.get("selector") or ""))
    if len(elements) > limit:
        parts.append("... and %d more" % (len(elements) - limit))
    return "available: " + "; ".join(parts)


def _match_element_by_text(elements: list[dict], needle: str) -> tuple:
    """
    Find the element a human would call *needle*.

    Returns ``(element, status)``:

      "exact"     -- one element carries exactly that label
      "partial"   -- one element's label contains it
      "ambiguous" -- several elements match; the element is None
      "none"      -- nothing matched; the element is None

    An ambiguous match is never resolved by guessing: clicking the wrong
    button is worse than asking once more.
    """
    want = str(needle or "").strip().lower()
    if not want:
        return None, "none"

    def label_of(el):
        return str(el.get("name") or "").strip().lower()

    exact = [el for el in elements if label_of(el) == want]
    if len(exact) == 1:
        return exact[0], "exact"
    if len(exact) > 1:
        return None, "ambiguous"

    starts = [el for el in elements if label_of(el).startswith(want)]
    if len(starts) == 1:
        return starts[0], "partial"
    if len(starts) > 1:
        return None, "ambiguous"

    contains = [el for el in elements if want in label_of(el)]
    if len(contains) == 1:
        return contains[0], "partial"
    if len(contains) > 1:
        return None, "ambiguous"

    typed = [el for el in elements
             if want == str(el.get("type") or "").strip().lower()
             or want == str(el.get("role") or "").strip().lower()]
    if len(typed) == 1:
        return typed[0], "exact"
    if len(typed) > 1:
        return None, "ambiguous"

    return None, "none"


def _click_element(ws, x: float, y: float, msg_id: int) -> bool:
    """Click at specific coordinates using CDP Input.dispatchMouseEvent."""
    # Mouse pressed
    _send_cdp(ws, "Input.dispatchMouseEvent", {
        "type": "mousePressed",
        "x": x, "y": y,
        "button": "left",
        "clickCount": 1
    }, msg_id)
    # Mouse released
    result = _send_cdp(ws, "Input.dispatchMouseEvent", {
        "type": "mouseReleased",
        "x": x, "y": y,
        "button": "left",
        "clickCount": 1
    }, msg_id + 1)
    return "error" not in result


def _type_text(ws, text: str, msg_id: int) -> bool:
    """Type text using CDP Input.dispatchKeyEvent."""
    for i, char in enumerate(text):
        _send_cdp(ws, "Input.dispatchKeyEvent", {
            "type": "char",
            "text": char,
            "unmodifiedText": char
        }, msg_id + i)
    return True


def _perform_actions(ws, actions: list, start_msg_id: int) -> tuple:
    """
    Perform a sequence of actions (click, input, wait, scroll, press, select,
    evaluate).

    Returns ``(success, error_msg, final_msg_id, results)`` where *results*
    is a list of human-readable strings -- one per action -- describing what
    happened (e.g. the return value of an ``evaluate`` action).
    """
    msg_id = start_msg_id
    results: list[str] = []
    for i, action in enumerate(actions):
        action_type = action.get("type", "click")
        selector = action.get("selector", "")
        wait_after = float(action.get("wait_after", 1.0))

        if action_type == "click":
            # Four ways to aim a click, ordered by how much the caller has to
            # know in advance:
            #   x/y       -- raw viewport pixels ("click exactly there")
            #   text      -- the visible label of the element ("click Submit")
            #   index     -- a 1-based index from a 'find' listing
            #   selector  -- a CSS selector, resolved against the live DOM
            # The label and the index are resolved by asking the page what it
            # actually has, so no coordinates are ever guessed.
            x_coord = action.get("x")
            y_coord = action.get("y")
            label = str(action.get("text") or "").strip()
            index = action.get("index")
            if x_coord is not None and y_coord is not None:
                cx, cy = float(x_coord), float(y_coord)
            else:
                if label or index is not None:
                    elements, msg_id = _enumerate_interactive(ws, msg_id)
                    if not elements:
                        return (False,
                                "no interactive elements found on the page"
                                + (f" — nothing to click for {label!r}" if label else ""),
                                msg_id, results)
                    if index is not None:
                        try:
                            position = int(index)
                        except (TypeError, ValueError):
                            return (False,
                                    f"click index {index!r} is not a number",
                                    msg_id, results)
                        if position < 1 or position > len(elements):
                            return (False,
                                    f"click index {position} is out of range — the "
                                    f"listing is 1-based and holds {len(elements)} "
                                    f"element(s). " + _candidate_hint(elements),
                                    msg_id, results)
                        chosen = elements[position - 1]
                    else:
                        chosen, status = _match_element_by_text(elements, label)
                        if status == "ambiguous":
                            return (False,
                                    f"{label!r} matches several elements — pick one "
                                    f"by index. " + _candidate_hint(elements),
                                    msg_id, results)
                        if chosen is None:
                            return (False,
                                    f"no element is labelled {label!r}. "
                                    + _candidate_hint(elements),
                                    msg_id, results)
                    selector = str(chosen.get("selector") or selector)
                if not selector:
                    return (False,
                            "Click action requires a selector, a label, an index "
                            "or x/y coordinates",
                            msg_id, results)
                info = _locate_element(ws, selector, msg_id)
                msg_id += 1  # the lookup used one id
                if not info.get("found"):
                    # Name what *is* on the page, so the next attempt is
                    # informed instead of another blind guess.
                    hints, msg_id = _enumerate_interactive(ws, msg_id)
                    return (False,
                            "Element not found for selector: " + str(selector)
                            + ". " + _candidate_hint(hints),
                            msg_id, results)
                if info.get("hidden"):
                    return (False, "Element is not visible on the page: " + str(selector),
                            msg_id, results)
                cx = (info.get("x") or 0.0) + (info.get("width") or 0.0) / 2
                cy = (info.get("y") or 0.0) + (info.get("height") or 0.0) / 2
                # The element was scrolled into view, so its centre ought to
                # be inside the viewport.  When it is not, the click would
                # land on empty space -- saying so is better than reporting
                # a click that never reached anything.
                vw, vh = info.get("vwidth"), info.get("vheight")
                if vw and vh and not (0 <= cx <= vw and 0 <= cy <= vh):
                    return (False,
                            "Element is outside the viewport at ("
                            + str(int(cx)) + ", " + str(int(cy)) + "): "
                            + str(selector),
                            msg_id, results)
            # A click is two CDP commands (press + release), so it has to
            # start at the current id -- starting one higher would reuse the
            # release id for the next command.
            if not _click_element(ws, cx, cy, msg_id):
                label = " (selector=" + str(selector) + ")" if selector else ""
                return (False, "Failed to click at (" + str(cx) + ", " + str(cy) + ")" + label,
                        msg_id, results)
            msg_id += 2
            label = " -- selector=" + selector if selector else ""
            results.append("clicked at (" + str(int(cx)) + ", " + str(int(cy)) + ")" + label)

        elif action_type == "input":
            # A field can be addressed by its visible label ("Email") as
            # well as by a selector, so the model does not need to know the
            # markup to fill a form in.
            field_label = str(action.get("label") or "").strip()
            if not selector and field_label:
                elements, msg_id = _enumerate_interactive(ws, msg_id)
                chosen, _status = _match_element_by_text(elements, field_label)
                if chosen is None:
                    return (False,
                            f"no field is labelled {field_label!r}. "
                            + _candidate_hint(elements),
                            msg_id, results)
                selector = str(chosen.get("selector") or "")
            if not selector:
                return (False, "Input action requires a selector or a label",
                        msg_id, results)
            # First click to focus.  The lookup scrolls the field into view
            # on the way, so the focus click lands on it even when the field
            # is below the fold.
            info = _locate_element(ws, selector, msg_id)
            msg_id += 1  # the lookup used one id
            if not info.get("found"):
                return (False, "Input element not found: " + str(selector),
                        msg_id, results)
            if info.get("hidden"):
                return (False,
                        "Input element is not visible on the page: " + str(selector),
                        msg_id, results)
            cx = (info.get("x") or 0.0) + (info.get("width") or 0.0) / 2
            cy = (info.get("y") or 0.0) + (info.get("height") or 0.0) / 2
            # The focus click takes two ids (press + release), the typing
            # starts right after them -- otherwise the two would share an id.
            _click_element(ws, cx, cy, msg_id)
            # Type the text
            text = action.get("text", "")
            _type_text(ws, text, msg_id + 2)
            msg_id += 2 + len(text)
            results.append("typed into " + selector + ": " + repr(text))

        elif action_type == "wait":
            # Just wait
            results.append("waited")

        elif action_type == "scroll":
            # Scroll the viewport: by a delta, to an absolute position, or to
            # the very bottom/top of the document.
            to = action.get("to")
            amount = action.get("amount", action.get("delta", 600))
            direction = str(action.get("direction", "down")).lower()
            if to is not None:
                try:
                    js = "window.scrollTo(0, %d);" % int(float(to))
                except (TypeError, ValueError):
                    js = "window.scrollTo(0, 0);"
            elif direction in ("bottom", "end"):
                js = "window.scrollTo(0, document.body.scrollHeight);"
            elif direction in ("top", "start"):
                js = "window.scrollTo(0, 0);"
            else:
                try:
                    delta = float(amount)
                except (TypeError, ValueError):
                    delta = 600.0
                if direction == "up":
                    delta = -abs(delta)
                elif direction not in ("down", "bottom"):
                    delta = abs(delta)
                js = "window.scrollBy(0, %d);" % int(delta)
            _evaluate_js(ws, js, msg_id=msg_id)
            msg_id += 1
            results.append("scrolled")

        elif action_type == "press":
            # Send a real key press (Enter, Tab, Escape, arrows, etc.)
            key = str(action.get("key", "Enter"))
            codes = {
                "Enter": (13, "Enter", "\r"),
                "Tab": (9, "Tab", ""),
                "Escape": (27, "Escape", ""),
                "Backspace": (8, "Backspace", ""),
                "ArrowDown": (40, "ArrowDown", ""),
                "ArrowUp": (38, "ArrowUp", ""),
                "ArrowLeft": (37, "ArrowLeft", ""),
                "ArrowRight": (39, "ArrowRight", ""),
                "PageDown": (34, "PageDown", ""),
                "PageUp": (33, "PageUp", ""),
            }
            vk, code, text = codes.get(key, (0, key, ""))
            params = {
                "type": "keyDown",
                "key": key,
                "code": code,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
            }
            if text:
                params["text"] = text
                params["unmodifiedText"] = text
            _send_cdp(ws, "Input.dispatchKeyEvent", params, msg_id)
            _send_cdp(ws, "Input.dispatchKeyEvent", {
                "type": "keyUp", "key": key, "code": code,
                "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
            }, msg_id + 1)
            msg_id += 2
            results.append("pressed key: " + key)

        elif action_type == "select":
            # Choose an option in a <select> and fire its change event.
            if not selector:
                return (False, "Select action requires a selector", msg_id, results)
            value = str(action.get("value", action.get("text", "")))
            safe_selector = selector.replace("'", "\\'")
            safe_value = value.replace("'", "\\'")
            js = (
                "(function(){var el=document.querySelector('" + safe_selector + "');"
                "if(!el) return 'not found';"
                "el.value='" + safe_value + "';"
                "el.dispatchEvent(new Event('change',{bubbles:true}));"
                "return 'ok';})()"
            )
            result, err = _evaluate_js(ws, js, msg_id=msg_id)
            msg_id += 1
            if err:
                results.append("select error: " + str(err))
            else:
                results.append("selected " + selector + " = " + value)

        elif action_type == "evaluate":
            # Run arbitrary JavaScript and surface the result.
            expr = action.get("expression", "")
            if not expr:
                return (False,
                        "Evaluate action requires an 'expression' field",
                        msg_id, results)
            result, err = _evaluate_js(ws, expr, msg_id=msg_id)
            msg_id += 1
            if err:
                results.append("evaluate error: " + str(err))
            else:
                display = str(result) if result is not None else ""
                if len(display) > 500:
                    display = display[:500] + " ...[truncated]"
                results.append("evaluate: " + display)

        elif action_type in ("find", "elements", "inspect"):
            # List what the page can be clicked.  The model picks a label or
            # an index from the listing instead of guessing pixels.
            query = str(action.get("query") or "").strip()
            elements, msg_id = _enumerate_interactive(ws, msg_id)
            if query:
                needle = query.lower()
                elements = [el for el in elements
                            if needle in str(el.get("name") or "").lower()
                            or needle in str(el.get("selector") or "").lower()
                            or needle == str(el.get("type") or "").lower()
                            or needle == str(el.get("role") or "").lower()]
            results.append(_format_elements(elements, query))

        else:
            return (False, "Unknown action type: " + str(action_type), msg_id, results)

        if wait_after > 0:
            time.sleep(wait_after)
            # Wait for any navigation/load triggered by the action
            _loaded, msg_id = _wait_for_load(ws, timeout=10, msg_id=msg_id)

    return (True, "", msg_id, results)


def _cleanup_chrome(proc) -> None:
    """Terminate Chrome subprocess and all its children, then clean up user data."""
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass


# ── visibility filtering (used by the no-JS fallback) ─────────────────────────

# Tags that never hold readable page text.
_NON_TEXT_TAGS = (
    "script", "style", "noscript", "template", "iframe", "object", "embed",
    "canvas", "audio", "video", "map", "link", "meta",
)

# An inline style that takes the element out of the visible page.
_HIDDEN_STYLE_RE = re.compile(
    r"(?:^|;)\s*(?:display\s*:\s*none\b|visibility\s*:\s*(?:hidden|collapse)\b)",
    re.I,
)

# A CSS rule: a selector list followed by a declaration block.
_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)

# A declaration block that hides whatever the rule selects.
_HIDE_DECL_RE = re.compile(
    r"(?:display\s*:\s*none\b|visibility\s*:\s*(?:hidden|collapse)\b)", re.I)


def _hidden_css_selectors(css_text: str) -> list:
    """Pull out the selectors of every rule that hides what it selects."""
    selectors = []
    for match in _CSS_RULE_RE.finditer(css_text or ""):
        selector_text = match.group(1).strip()
        declarations = match.group(2)
        if not selector_text or not _HIDE_DECL_RE.search(declarations):
            continue
        # '@media ...' is a keyword, not a selector.  The rule regex above
        # already peels the inner rule out of a media block, so whatever is
        # left starting with '@' cannot be matched against the soup.
        if selector_text.startswith("@"):
            continue
        selectors.append(selector_text)
    return selectors


def _drop_elements(elements) -> None:
    """Remove a set of elements, skipping ones already taken apart."""
    for element in elements:
        try:
            if getattr(element, "decomposed", False):
                continue
            element.decompose()
        except Exception:
            continue


def _strip_hidden_content(soup, base_url: str = "", headers: dict = None) -> None:
    """
    Remove everything a sighted visitor would not see.

    A page fetched without a browser still carries every translation, every
    closed dialog and every collapsed menu in its markup, because the script
    that hides them never ran.  Dumping that markup verbatim is how a scraper
    ends up reporting six languages at once for a page that shows one.

    Three kinds of hiding are handled:
      * the ``hidden`` attribute,
      * inline styles (``display:none`` / ``visibility:hidden``),
      * stylesheet rules that hide the elements they select -- including
        same-origin stylesheets linked from the page, because the class that
        hides a translation is usually defined there and nowhere else.
    """
    # The stylesheets have to be read before the <style> tags are removed.
    css_texts = [style.get_text() for style in soup.find_all("style")]
    for link in soup.find_all("link"):
        rel = link.get("rel") or []
        if isinstance(rel, str):
            rel = [rel]
        if "stylesheet" not in [str(r).lower() for r in rel]:
            continue
        href = link.get("href")
        if not href:
            continue
        url = urljoin(base_url, href) if base_url else href
        if not url.startswith(("http://", "https://")):
            continue
        try:
            response = requests.get(url, headers=headers or {}, timeout=6)
            if response.status_code == 200:
                css_texts.append(response.text)
        except Exception:
            continue

    selectors = []
    for css_text in css_texts:
        selectors.extend(_hidden_css_selectors(css_text))

    # Tags that are never page text.
    _drop_elements(soup.find_all(_NON_TEXT_TAGS))
    _drop_elements(soup.find_all(["nav", "footer", "header"]))

    # The 'hidden' attribute.
    _drop_elements(soup.find_all(attrs={"hidden": True}))

    # Inline styles that hide the element.
    for element in soup.find_all(style=True):
        if _HIDDEN_STYLE_RE.search(element.get("style") or ""):
            _drop_elements([element])

    # Stylesheet rules that hide the elements they select.
    for selector in selectors:
        try:
            matched = soup.select(selector)
        except Exception:
            continue
        _drop_elements(matched)


def _fallback_selector(el) -> str:
    """A best-effort CSS selector for an element of a page fetched raw."""
    if el.get("id"):
        return "#" + str(el["id"])
    name = el.get("name")
    if name:
        return '%s[name="%s"]' % (el.name, name)
    classes = el.get("class")
    if classes:
        first = str(classes).split()[0]
        return "%s.%s" % (el.name, first)
    return str(el.name)


def _fallback_label(el) -> str:
    """The text a human would read for a raw element."""
    text = el.get_text(separator=" ", strip=True)
    if not text:
        text = (el.get("value") or el.get("placeholder")
                or el.get("aria-label") or el.get("title") or "")
    return str(text).strip()[:80]


def _fallback_interactive(soup) -> list:
    """The clickable things in a page that was fetched without a browser."""
    found = []
    for el in soup.select("button, a[href], input, textarea, select, "
                          "[role=button], [onclick]"):
        if el.has_attr("disabled"):
            continue
        found.append({
            "tag": str(el.name),
            "text": _fallback_label(el),
            "selector": _fallback_selector(el),
        })
    return found


def _fallback_match_text(soup, label):
    """Find a raw element by the label a human would read."""
    want = str(label or "").strip().lower()
    if not want:
        return None
    exact, partial = [], []
    for el in soup.select("button, a[href], input, textarea, select, "
                          "[role=button], [onclick]"):
        text = _fallback_label(el).lower()
        if text == want:
            exact.append(el)
        elif want in text:
            partial.append(el)
    if len(exact) == 1:
        return exact[0]
    if len(partial) == 1:
        return partial[0]
    return None


def _web_scrape_fallback(url: str, selector: str = None, max_chars: int = 12000, actions: list = None) -> str:
    """
    Fallback scraper using requests + BeautifulSoup.
    Used when Chrome/Chromium is not available.

    Best-effort handling of actions:
      - input: fills the form field and submits the enclosing form (POST or GET).
      - click: if the element is an <a> link, follows its href to fetch the target URL.
      - wait:  no-op (pass-through).
    Actions that cannot be fulfilled are noted in the output, and text extraction
    proceeds on whatever page state is available.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
        action_notes = []

        # Process actions before fetching the final page
        if actions:
            for i, action in enumerate(actions):
                atype = action.get("type", "click")
                aselect = action.get("selector", "")
                atext = action.get("text", "")
                aw = float(action.get("wait_after", 1.0))

                if atype == "wait":
                    # Best-effort: just sleep
                    time.sleep(min(aw, 3.0))
                    continue

                # Fetch the page so we can find the element
                resp = requests.get(url, headers=headers, timeout=12)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")

                if atype == "find":
                    # List the clickable things, so a click can be aimed by
                    # label instead of by guessed coordinates.
                    elements = _fallback_interactive(soup)
                    if not elements:
                        action_notes.append("[interactive elements] none found")
                    else:
                        lines = ["[interactive elements]"]
                        for idx, el in enumerate(elements, 1):
                            lines.append('  [%d] %s "%s" (%s)' % (
                                idx, el["tag"], el["text"], el["selector"]))
                        action_notes.append("\n".join(lines))
                    continue

                if atype == "input":
                    # Find the input element and submit its enclosing form
                    el = soup.select_one(aselect) if aselect else None
                    if el is None and action.get("label"):
                        el = _fallback_match_text(soup, action.get("label"))
                    if el is None:
                        action_notes.append(
                            f"[WARNING] Input element not found: "
                            f"{aselect or action.get('label') or '(no target given)'}")
                        continue
                    # Look for an enclosing <form>
                    form = el.find_parent("form") if el else None
                    if form is None:
                        action_notes.append(f"[WARNING] Input element has no enclosing form: {aselect}")
                        continue

                    # Build form data from all inputs in the form
                    form_data = {}
                    form_inputs = form.select("input, textarea, select")
                    for inp in form_inputs:
                        name = inp.get("name", "")
                        if not name:
                            continue
                        if inp == el:
                            form_data[name] = atext
                        else:
                            # Use existing value or empty
                            form_data[name] = inp.get("value", "")

                    # Determine action method and target
                    method = form.get("method", "GET").upper()
                    action_url = form.get("action") or url
                    if not action_url:
                        action_url = url
                    # Resolve relative URLs
                    action_url = urljoin(url, action_url)

                    if method == "POST":
                        resp = requests.post(action_url, headers=headers, data=form_data, timeout=12)
                        url = action_url  # Update URL for subsequent actions
                    else:
                        resp = requests.get(action_url, headers=headers, params=form_data, timeout=12)
                        url = action_url

                    resp.raise_for_status()
                    # Update soup for subsequent actions
                    soup = BeautifulSoup(resp.text, "html.parser")

                    if aw > 0:
                        time.sleep(min(aw, 3.0))
                    continue

                if atype == "click":
                    # Try to follow links
                    el = soup.select_one(aselect) if aselect else None
                    if el is None and atext:
                        el = _fallback_match_text(soup, atext)
                    if el is None:
                        action_notes.append(
                            f"[WARNING] Click target not found: "
                            f"{aselect or repr(atext)}")
                        continue

                    # Check if it's a link or a button
                    if el.name == "a" or el.get("href"):
                        href = el.get("href")
                        if href:
                            new_url = urljoin(url, href)
                            url = new_url
                            resp = requests.get(url, headers=headers, timeout=12)
                            resp.raise_for_status()
                            soup = BeautifulSoup(resp.text, "html.parser")
                        else:
                            action_notes.append(f"[WARNING] Click element has no href: {aselect}")
                    elif el.name == "button" and el.get("onclick"):
                        # Can't execute JS in fallback - note limitation
                        action_notes.append(f"[WARNING] Button with onclick handler cannot be executed in fallback mode: {aselect}")
                    else:
                        action_notes.append(f"[NOTE] Click action on non-link element in fallback mode: {aselect}")

                    if aw > 0:
                        time.sleep(min(aw, 3.0))
                    continue

                if atype not in ("click", "input", "wait", "find"):
                    action_notes.append(f"[WARNING] Unknown action type: {atype}")

        # Final fetch and extraction — use the last response from the actions loop
        # if available, otherwise do a fresh GET.
        if actions and 'resp' in dir() and resp is not None:
            # Use the last response from the actions loop
            pass
        else:
            resp = requests.get(url, headers=headers, timeout=12)
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # Drop everything a visitor would not see before pulling the text
        # out.  Without this the output contains every translation and every
        # collapsed panel on the page, as if all of them were visible at once.
        _strip_hidden_content(soup, base_url=url, headers=headers)

        if selector:
            target = soup.select(selector)
            text = "\n".join(el.get_text(separator="\n", strip=True) for el in target)
        else:
            text = soup.get_text(separator="\n", strip=True)

        # Prepend action notes if any
        if action_notes:
            notes_str = "\n".join(action_notes)
            text = notes_str + "\n" + text

        return text[:max_chars] if len(text) > max_chars else text
    except Exception as e:
        return f"[ERROR] Web scrape failed: {e}"





def _format_action_results(results: list[str]) -> str:
    """Render per-action results as a readable block for the model."""
    if not results:
        return ""
    lines = ["--- action results ---"]
    for i, r in enumerate(results, 1):
        lines.append("  " + str(i) + ". " + r)
    return "\n".join(lines)

# ── PERSISTENT BROWSER SESSIONS ──────────────────────────────────────────────
# A browser session owns a Chromium process plus its DevTools websocket.  The
# session outlives a single tool call, so the model can open a page, click
# through it, and keep the same page state — the scroll position, the
# cookies, and anything already typed into a form.
#
# Sessions are addressed by a short id ("b1", "b2", …) that the open call
# returns and every later call has to pass back.

_BROWSER_SESSIONS: dict[str, dict] = {}
_BROWSER_SESSION_SEQ = 0
_BROWSER_LOCK = threading.Lock()

# A session nobody has touched for this long is reaped, so an abandoned
# browser cannot leak a Chromium process forever.
_BROWSER_IDLE_TIMEOUT = 300.0

# Screenshots are downscaled before they reach the model: a full-resolution
# PNG easily reaches a megabyte, which would eat a large slice of the
# context window for no extra understanding.
_SCREENSHOT_MAX_WIDTH = 1024
_SCREENSHOT_MAX_HEIGHT = 1024
_SCREENSHOT_QUALITY = 70

# The browser keeps its profile directories and screenshots under the
# project workspace: /tmp is not writable on Termux and does not exist on
# Windows, so a hardcoded temp path is not portable.
_BROWSER_DIR = os.path.join(paths.WORKSPACE_DIR, ".browser")
_SCREENSHOT_DIR = os.path.join(paths.WORKSPACE_DIR, "screenshots")


# ── IMAGE ATTACHMENTS (tool → model) ─────────────────────────────────────────
# A tool cannot hand an image back through its normal string return value, so
# tools that produce one (a browser screenshot, a generated image) queue it
# here.  core/llm_client.py drains the queue after every tool call and
# attaches the images to the next request — that is how a multimodal model
# actually *sees* them.

_IMAGE_ATTACHMENTS: list[dict] = []
_IMAGE_ATTACHMENT_LOCK = threading.Lock()

# path -> (mtime, data URI).  Screenshots are re-encoded for every request
# that still references them, so the encoded form is cached.
_IMAGE_DATA_URI_CACHE: dict[str, tuple] = {}


def queue_image_attachment(path: str, label: str = "") -> str:
    """Queue an image file so the next LLM request can include it.

    Returns the absolute path that was queued, or "" when the file does not
    exist (a missing screenshot must never break the tool call that produced
    it).
    """
    if not path:
        return ""
    try:
        if not os.path.isfile(path):
            return ""
        absolute = os.path.abspath(path)
    except Exception:
        return ""
    with _IMAGE_ATTACHMENT_LOCK:
        _IMAGE_ATTACHMENTS.append({
            "path": absolute,
            "label": label or os.path.basename(absolute),
        })
    return absolute


def pop_image_attachments() -> list[dict]:
    """Return and clear the queued image attachments."""
    with _IMAGE_ATTACHMENT_LOCK:
        pending = list(_IMAGE_ATTACHMENTS)
        _IMAGE_ATTACHMENTS.clear()
    return pending


def image_to_data_uri(path: str, max_bytes: int = 900_000) -> str | None:
    """Read an image file and return it as a base64 data URI.

    A file above ``max_bytes`` is rejected instead of silently truncated: a
    partial image is useless to the model and an unbounded one would blow
    the context budget.

    The result is cached, because the same screenshot is encoded again for
    every request that still carries it in the history.
    """
    try:
        if not os.path.isfile(path):
            return None
        mtime = os.path.getmtime(path)
    except Exception:
        return None

    cached = _IMAGE_DATA_URI_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        if os.path.getsize(path) > max_bytes:
            return None
        with open(path, "rb") as fh:
            raw = fh.read()
    except Exception:
        return None
    if not raw:
        return None
    if raw[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif raw[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/jpeg"
    uri = f"data:{mime};base64," + base64.b64encode(raw).decode()

    if len(_IMAGE_DATA_URI_CACHE) >= 16:
        _IMAGE_DATA_URI_CACHE.clear()
    _IMAGE_DATA_URI_CACHE[path] = (mtime, uri)
    return uri


def _browser_session_list() -> str:
    """Human-readable list of the currently open browser sessions."""
    with _BROWSER_LOCK:
        sessions = list(_BROWSER_SESSIONS.values())
    if not sessions:
        return "No browser sessions are open."
    lines = []
    for s in sessions:
        age = int(time.time() - s.get("last_used", time.time()))
        lines.append(
            f"  {s.get('id')}  url={s.get('url', '?')}  "
            f"title={s.get('title', '?')!r}  idle={age}s  "
            f"screenshots={s.get('screenshots', 0)}"
        )
    return "Open browser sessions:\n" + "\n".join(lines)


def _browser_session_health(session: dict) -> dict:
    """Probe whether a session's Chromium process is still alive."""
    proc = session.get("proc")
    alive = True
    if proc is not None:
        alive = proc.poll() is None
    return {
        "process_alive": alive,
        "created": session.get("created", 0),
        "last_used": session.get("last_used", 0),
        "url": session.get("url", ""),
        "title": session.get("title", ""),
        "port": session.get("port", 0),
        "screenshots": session.get("screenshots", 0),
    }


def _browser_launch_args(chrome_path: str, port: int, user_data_dir: str) -> list:
    """Chromium command line for a headless, scriptable session."""
    return [
        chrome_path,
        '--headless=new',
        '--no-sandbox',
        '--disable-gpu',
        '--disable-dev-shm-usage',
        '--disable-extensions',
        '--disable-sync',
        '--disable-translate',
        '--no-first-run',
        '--no-default-browser-check',
        '--mute-audio',
        '--disable-background-networking',
        '--disable-default-apps',
        '--hide-scrollbars',
        '--disable-features=PrivacySandboxSettings4',
        '--window-size=1024,768',
        f'--remote-debugging-port={port}',
        '--remote-allow-origins=*',
        f'--user-data-dir={user_data_dir}',
        'about:blank',
    ]


def _browser_update_state(session: dict) -> None:
    """Refresh the session's cached URL and page title."""
    ws = session.get("ws")
    if not ws:
        return
    mid = session["msg_id"]
    session["msg_id"] = mid + 1
    js = ("(function(){return {url: location.href, "
          "title: document.title};})()")
    value, err = _evaluate_js(ws, js, msg_id=mid)
    if err or not value:
        return
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return
    if isinstance(data, dict):
        session["url"] = data.get("url") or session.get("url", "")
        session["title"] = data.get("title") or session.get("title", "")


def _browser_extract_text(session: dict, selector: str = "") -> str:
    """Pull the readable text out of the page, optionally via a selector."""
    ws = session.get("ws")
    if not ws:
        return "[ERROR] Browser session has no DevTools connection."
    mid = session["msg_id"]
    session["msg_id"] = mid + 1
    if selector:
        safe_selector = selector.replace("'", "\\'")
        js = (
            f"(function() {{ var els = document.querySelectorAll('{safe_selector}');"
            f"if (!els || els.length === 0) {{ return '[WARNING] Selector \"{safe_selector}\" matched no elements'; }}"
            f"var texts = [];"
            f"for (var i = 0; i < els.length; i++) {{"
            f"  var el = els[i];"
            f"  var t = el.innerText || el.textContent || '';"
            # A form control's current value is not part of its text, so it
            # has to be asked for separately — otherwise reading back a field
            # the model just typed into returns nothing at all.
            f"  if (el.value !== undefined && el.value !== null && el.value !== '') {{"
            f"    t = (t ? t + '\\n' : '') + '[value: ' + el.value + ']';"
            f"  }}"
            f"  texts.push(t);"
            f"}}"
            f"return texts.join('\\n'); }})()"
        )
    else:
        js = (
            "(function() { var body = document.body;"
            "if (!body) { return document.documentElement.innerText || document.documentElement.textContent || ''; }"
            "return body.innerText || body.textContent || ''; }())"
        )
    text, err = _evaluate_js(ws, js, msg_id=mid)
    if err:
        return f"[ERROR] JS extraction failed: {err}"
    return text if isinstance(text, str) else (str(text) if text else "")


def _browser_extract_html(session: dict, selector: str = "") -> str:
    """Pull the raw HTML from the live page, optionally via a selector.

    Unlike ``_browser_extract_text`` (which normalises to readable text),
    this returns ``outerHTML`` so the caller can see the actual DOM structure
    -- tags, attributes, form values, data-* values, etc.
    """
    ws = session.get("ws")
    if not ws:
        return "[ERROR] Browser session has no DevTools connection."
    mid = session["msg_id"]
    session["msg_id"] = mid + 1
    if selector:
        safe_selector = selector.replace("'", "\\'")
        js = (
            "(function(){var els=document.querySelectorAll('" + safe_selector + "');"
            + "if(!els||els.length===0){return '[WARNING] Selector \""
            + safe_selector + "\" matched no elements';}"
            + "var hs=[];for(var i=0;i<els.length;i++){hs.push(els[i].outerHTML);}"
            # The newline has to survive as the two characters \n: a raw
            # newline inside a JS string literal is a syntax error, which
            # used to break every selector-scoped HTML extraction.
            + "return hs.join('\\n');})()"
        )
    else:
        js = "document.documentElement.outerHTML.toString()"
    text, err = _evaluate_js(ws, js, msg_id=mid)
    if err:
        return f"[ERROR] HTML extraction failed: {err}"
    return text if isinstance(text, str) else (str(text) if text else "")


# Screenshots are only useful for the turn that captured them (and the few
# turns after it), so the oldest ones are pruned instead of filling the
# device storage.
_SCREENSHOT_KEEP = 60


def _prune_screenshots() -> None:
    """Delete the oldest screenshots once the retention limit is exceeded."""
    try:
        entries = [
            os.path.join(_SCREENSHOT_DIR, n)
            for n in os.listdir(_SCREENSHOT_DIR)
            if n.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
        ]
    except OSError:
        return
    if len(entries) <= _SCREENSHOT_KEEP:
        return
    try:
        entries.sort(key=os.path.getmtime)
    except OSError:
        return
    for stale in entries[:len(entries) - _SCREENSHOT_KEEP]:
        try:
            os.remove(stale)
        except OSError:
            pass


def _browser_screenshot(session: dict, label: str = "screenshot") -> str | None:
    """Capture the current viewport and return the path of the saved file."""
    ws = session.get("ws")
    if not ws:
        return None
    mid = session["msg_id"]
    session["msg_id"] = mid + 1
    resp = _send_cdp(ws, "Page.captureScreenshot",
                     {"format": "jpeg", "quality": _SCREENSHOT_QUALITY},
                     mid, timeout=25)
    data = resp.get("result", {}).get("data")
    if not data:
        return None
    try:
        raw = base64.b64decode(data)
    except Exception:
        return None
    if not raw:
        return None

    os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
    safe_label = "".join(c for c in (label or "screenshot") if c.isalnum()
                         or c in "-_") or "screenshot"
    name = f"{safe_label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    path = os.path.join(_SCREENSHOT_DIR, name + ".jpg")
    _prune_screenshots()

    # Downscale so the picture stays small enough to put in a prompt.
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((_SCREENSHOT_MAX_WIDTH, _SCREENSHOT_MAX_HEIGHT))
        img.save(path, "JPEG", quality=_SCREENSHOT_QUALITY, optimize=True)
    except Exception:
        with open(path, "wb") as fh:
            fh.write(raw)

    session["screenshots"] = session.get("screenshots", 0) + 1
    return path


def _browser_destroy(session: dict) -> None:
    """Tear a session down: close the socket, kill Chromium, drop the profile."""
    ws = session.get("ws")
    if ws:
        try:
            ws.close()
        except Exception:
            pass
        session["ws"] = None
    _cleanup_chrome(session.get("proc"))
    session["proc"] = None
    user_data_dir = session.get("user_data_dir")
    if user_data_dir and os.path.exists(user_data_dir):
        shutil.rmtree(user_data_dir, ignore_errors=True)


def _browser_close(browser_id: str) -> str:
    """Close one browser session by id."""
    if not browser_id:
        return "[ERROR] browser(action='close') requires a browser_id."
    with _BROWSER_LOCK:
        session = _BROWSER_SESSIONS.pop(browser_id, None)
    if not session:
        return (f"[ERROR] No open browser session with id '{browser_id}'. "
                "Call browser(action='list') to see the open sessions.")
    _browser_destroy(session)
    return f"Closed browser session '{browser_id}'."


def _browser_close_all() -> None:
    """Close every open session (used on interpreter shutdown)."""
    with _BROWSER_LOCK:
        sessions = list(_BROWSER_SESSIONS.values())
        _BROWSER_SESSIONS.clear()
    for session in sessions:
        try:
            _browser_destroy(session)
        except Exception:
            pass


def _browser_reap_idle() -> None:
    """Close sessions that have been idle for longer than the timeout."""
    now = time.time()
    stale = []
    with _BROWSER_LOCK:
        for bid, session in list(_BROWSER_SESSIONS.items()):
            if now - session.get("last_used", now) > _BROWSER_IDLE_TIMEOUT:
                stale.append(_BROWSER_SESSIONS.pop(bid))
    for session in stale:
        try:
            _browser_destroy(session)
        except Exception:
            pass


try:  # pragma: no cover - interpreter shutdown hook
    import atexit as _atexit
    _atexit.register(_browser_close_all)
except Exception:
    pass


def _browser_get(browser_id: str) -> dict | None:
    """Look up a live session, reaping it if Chromium died on its own."""
    if not browser_id:
        return None
    with _BROWSER_LOCK:
        session = _BROWSER_SESSIONS.get(browser_id)
    if not session:
        return None
    proc = session.get("proc")
    if proc is not None and proc.poll() is not None:
        _browser_close(browser_id)
        return None
    session["last_used"] = time.time()
    return session


def _browser_start(url: str = "") -> tuple[dict | None, str]:
    """Launch Chromium, attach to its tab and optionally navigate.

    Returns ``(session, error_message)``; exactly one of the two is set.
    """
    chrome_path = _find_chrome()
    if not chrome_path:
        return None, ("[ERROR] Chromium/Chrome was not found on this system. "
                      "Install it (pkg install chromium) to use the browser tools.")
    port = _find_free_port()
    if not port:
        return None, "[ERROR] Could not find a free TCP port for Chromium."

    try:
        os.makedirs(_BROWSER_DIR, exist_ok=True)
    except Exception as exc:
        return None, f"[ERROR] Could not create the browser profile directory: {exc}"
    user_data_dir = os.path.join(_BROWSER_DIR, f"session_{port}_{os.getpid()}")

    proc = None
    ws = None
    try:
        proc = subprocess.Popen(
            _browser_launch_args(chrome_path, port, user_data_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if not _wait_for_port(port, timeout=20):
            _cleanup_chrome(proc)
            shutil.rmtree(user_data_dir, ignore_errors=True)
            return None, (f"[ERROR] Chrome did not start listening on port "
                          f"{port} within 20s")

        # The tab list can be empty for a moment right after the port opens
        # (and the first entry is not always the page we want), so poll for
        # a usable page target instead of failing on the first look.
        tabs_json = []
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            try:
                tabs_json = requests.get(
                    f'http://127.0.0.1:{port}/json', timeout=8).json() or []
            except Exception:
                tabs_json = []
            if any(t.get("webSocketDebuggerUrl") for t in tabs_json):
                break
            time.sleep(0.3)

        pages = [t for t in tabs_json if t.get("webSocketDebuggerUrl")]
        if not pages:
            _cleanup_chrome(proc)
            shutil.rmtree(user_data_dir, ignore_errors=True)
            return None, "[ERROR] Chrome started but exposed no page target"
        # Prefer a real "page" target over devtools/background entries.
        pages.sort(key=lambda t: t.get("type") != "page")

        import websocket as _ws_module
        ws = _ws_module.create_connection(pages[0]['webSocketDebuggerUrl'],
                                          timeout=30)
    except ImportError:
        _cleanup_chrome(proc)
        shutil.rmtree(user_data_dir, ignore_errors=True)
        return None, ("[ERROR] The browser tools need the 'websocket-client' "
                      "package (pip install websocket-client).")
    except Exception as exc:
        _cleanup_chrome(proc)
        shutil.rmtree(user_data_dir, ignore_errors=True)
        return None, f"[ERROR] Could not start the browser: {exc}"

    session = {
        "id": "", "proc": proc, "port": port, "ws": ws,
        "user_data_dir": user_data_dir, "url": url, "title": "",
        "created": time.time(), "last_used": time.time(),
        "msg_id": 4, "screenshots": 0,
    }
    _send_cdp(ws, "Runtime.enable", {}, 1)
    _send_cdp(ws, "Page.enable", {}, 2)

    if url:
        nav = _send_cdp(ws, "Page.navigate", {"url": url}, session["msg_id"])
        session["msg_id"] += 1
        if "error" in nav:
            err = nav.get("error", {})
            err_msg = err.get("message", "unknown") if isinstance(err, dict) else str(err)
            _browser_destroy(session)
            return None, f"[ERROR] Navigation to {url} failed: {err_msg}"
        _loaded, session["msg_id"] = _wait_for_load(
            ws, timeout=15, msg_id=session["msg_id"])
        time.sleep(0.8)

    _browser_update_state(session)
    return session, ""


def _browser_header(session: dict, screenshot_path: str | None = None) -> str:
    """The status line every browser result starts with."""
    lines = [f"[Browser {session.get('id')}] {session.get('url') or 'about:blank'}"]
    title = session.get("title")
    if title:
        lines[0] += f" — {title}"
    if screenshot_path:
        lines.append(f"[Screenshot saved to {screenshot_path} — "
                     "attached to this reply as an image]")
    return "\n".join(lines)


def browser(
    action: str = "open",
    url: str = "",
    browser_id: str = "",
    selector: str = "",
    actions: list | None = None,
    max_chars: int = 12000,
    screenshot: bool = True,
    extract_html: bool = False,
) -> str:
    """Drive a persistent, reusable browser session.

    A browser session is a real Chromium process that stays alive between
    tool calls, so the model can open a page, click through it, and keep the
    same page state.  The session is addressed by the short id returned by
    the ``open`` action.

    Actions:
      * ``open``      -- launch a browser, navigate to ``url``, return the id.
      * ``act``       -- run ``actions`` (click/input/wait/scroll/press/select/
                        evaluate/find) in an existing session and return the new
                        page state.  Click can target an element by its visible
                        label (``text``), by its index in a ``find`` listing, by
                        a CSS selector **or** by raw ``x``/``y`` viewport
                        coordinates.  A ``find`` action lists the page's
                        buttons, links and fields with their labels and
                        ready-to-use selectors, so a click never has to guess
                        a position.
      * ``navigate``  -- load a new URL in an existing session.
      * ``screenshot``-- capture the current viewport.
      * ``state``     -- report URL, title, session health and page text.
      * ``close``     -- terminate the browser and free its resources.
      * ``list``      -- list the open sessions.

    When ``screenshot`` is true the viewport is captured and the picture is
    attached to the reply, so a multimodal model can look at the page.

    When ``extract_html`` is true the raw ``outerHTML`` of the page (or of the
    element matched by ``selector``) is also returned, so the model can see
    the actual DOM structure, attributes and data-* values.
    """
    global _BROWSER_SESSION_SEQ

    action = (action or "open").strip().lower()
    max_chars = max(1000, min(50000, int(max_chars or 12000)))
    selector = selector or ""

    if action == "list":
        return _browser_session_list()

    if action == "open":
        url = (url or "").strip()
        if not url:
            return "[ERROR] browser(action='open') requires a url."
        session, err = _browser_start(url)
        if not session:
            return err
        with _BROWSER_LOCK:
            _BROWSER_SESSION_SEQ += 1
            session["id"] = "b" + str(_BROWSER_SESSION_SEQ)
            _BROWSER_SESSIONS[session["id"]] = session
        action_results: list[str] = []
        if actions:
            ok, msg, mid, action_results = _perform_actions(
                session["ws"], actions, session["msg_id"])
            session["msg_id"] = mid
            if not ok:
                return "[ERROR] Action failed: " + msg
        _browser_update_state(session)
        text = _browser_extract_text(session, selector)
        shot = _browser_screenshot(session, label=session["id"]) if screenshot else None
        if shot:
            queue_image_attachment(shot, "screenshot of " + str(session.get("url", "")))
        if len(text) > max_chars:
            text = text[:max_chars]
        html_text = ""
        if extract_html:
            html_text = _browser_extract_html(session, selector)
        out = (_browser_header(session, shot)
               + "\n--- page text ---\n" + text
               + ("\n--- page html ---\n" + html_text if extract_html else "")
               + (_format_action_results(action_results) if action_results else "")
               + ("\n[Keep using browser_id='" + session["id"]
                  + "' to interact with this page.]"
                  if session.get("id") else ""))
        return out

    if action in ("act", "interact", "navigate", "screenshot", "state", "close"):
        if action == "close":
            return _browser_close(browser_id)

        session = _browser_get(browser_id)
        if not session:
            return ("[ERROR] No open browser session with id '" + str(browser_id)
                    + "'. Use browser(action='open', url=...) first, or "
                    "browser(action='list') to see the open sessions.")

        action_results: list[str] = []

        if action == "navigate":
            url = (url or "").strip()
            if not url:
                return "[ERROR] browser(action='navigate') requires a url."
            nav = _send_cdp(session["ws"], "Page.navigate", {"url": url},
                            session["msg_id"])
            session["msg_id"] += 1
            if "error" in nav:
                err = nav.get("error", {})
                err_msg = err.get("message", "unknown") if isinstance(err, dict) else str(err)
                return "[ERROR] Navigation to " + url + " failed: " + err_msg
            _wait_for_load(session["ws"], timeout=15)
            time.sleep(0.6)

        elif action == "screenshot":
            shot = _browser_screenshot(session, label=browser_id)
            if not shot:
                return "[ERROR] Screenshot capture failed."
            queue_image_attachment(shot, "screenshot of " + str(session.get("url", "")))
            return _browser_header(session, shot)

        elif action == "state":
            # Report session health, optionally with screenshot, text and HTML.
            health = _browser_session_health(session)
            lines = [
                "session: " + str(session.get("id", "?")),
                "process: " + ("alive" if health["process_alive"] else "DEAD"),
                "url: " + str(health["url"] or "?"),
                "title: " + str(health["title"] or "(untitled)"),
                "port: " + str(health["port"]),
                "screenshots taken: " + str(health["screenshots"]),
            ]
            from datetime import datetime as _dt
            if health["created"]:
                lines.append("created: " + _dt.fromtimestamp(health["created"]).isoformat())
            if health["last_used"]:
                lines.append("idle: " + str(int(time.time() - health["last_used"])) + "s ago")
            if screenshot:
                shot = _browser_screenshot(session, label=browser_id)
                if shot:
                    queue_image_attachment(shot, "screenshot of " + str(session.get("url", "")))
                    lines.append(_browser_header(session, shot))
            text = _browser_extract_text(session, selector)
            if len(text) > max_chars:
                text = text[:max_chars]
            lines.append("\n--- page text ---\n" + text)
            if extract_html:
                html_text = _browser_extract_html(session, selector)
                lines.append("\n--- page html ---\n" + html_text)
            return "\n".join(lines)

        else:
            # act / interact: perform the actions, then show the updated
            # page state.  The action results have to travel with the page
            # text -- an evaluate action's return value lives there.
            if not actions:
                return ("[ERROR] browser(action='act') requires an actions list "
                        "(click/input/wait/scroll/press/select/evaluate).")
            ok, msg, mid, action_results = _perform_actions(
                session["ws"], actions, session["msg_id"])
            session["msg_id"] = mid
            if not ok:
                return "[ERROR] Action failed: " + msg

        # The page state that 'act' and 'navigate' share: where the browser
        # is now, what it says, and what the actions did.  Both actions end
        # here -- falling out of the branch instead would report the action
        # as unknown even though it had already been carried out.
        _browser_update_state(session)
        text = _browser_extract_text(session, selector)
        shot = _browser_screenshot(session, label=browser_id) if screenshot else None
        if shot:
            queue_image_attachment(shot, "screenshot of " + str(session.get("url", "")))
        if len(text) > max_chars:
            text = text[:max_chars]
        html_text = ""
        if extract_html:
            html_text = _browser_extract_html(session, selector)
        return (_browser_header(session, shot)
                + "\n--- page text ---\n" + text
                + ("\n--- page html ---\n" + html_text if extract_html else "")
                + (_format_action_results(action_results) if action_results else ""))

    return ("[ERROR] Unknown browser action '" + str(action) + "'. "
            "Use one of: open, act, navigate, screenshot, state, close, list.")

def browse_web(url: str, selector: str = "", max_chars: int = 12000,
               actions: list = None, screenshot: bool = True,
               extract_html: bool = False) -> str:
    """
    Fetch a URL and convert readable content into structured text.

    Uses a headless Chromium browser via the Chrome DevTools Protocol to
    properly render JavaScript-heavy pages, single-page applications, and
    dynamically loaded content. Supports CSS selectors to target specific
    elements and a sequence of actions (click, wait, input, scroll, press,
    select, evaluate, find) for interaction.  The ``find`` action lists the
    page's clickable elements with their labels and ready-to-use selectors, so
    a click can be aimed by name or by index instead of by guessed
    coordinates. Falls back to requests+BeautifulSoup if
    Chromium is unavailable.

    The browser is closed again when the call finishes.  To keep a page
    alive across calls — and to click through it step by step — use the
    ``browser`` tool instead, which hands back a session id.

    Args:
        url:          The URL to fetch.
        selector:     Optional CSS selector to target specific element(s).
        max_chars:    Maximum characters of extracted text (default 12000).
        actions:      Optional list of actions to perform before extraction.
        screenshot:   Capture the viewport and attach it to the reply so a
                      multimodal model can look at the page (default True).
        extract_html: Also return the raw outerHTML of the page or matched
                      element(s) so the model can see the DOM structure
                      (default False).
    """
    max_chars = max(1000, min(50000, int(max_chars or 12000)))

    session, err = _browser_start((url or "").strip())
    if not session:
        # Chromium is unavailable or refused to start: fall back to the
        # plain HTTP scraper rather than failing the call outright.
        return _web_scrape_fallback(url, selector, max_chars, actions)

    try:
        action_results: list[str] = []
        if actions:
            ok, msg, mid, action_results = _perform_actions(
                session["ws"], actions, session["msg_id"])
            session["msg_id"] = mid
            if not ok:
                return f"[ERROR] Action failed: {msg}"

        _browser_update_state(session)
        text = _browser_extract_text(session, selector)
        if len(text) > max_chars:
            text = text[:max_chars]

        shot = _browser_screenshot(session, label="browse") if screenshot else None
        if shot:
            queue_image_attachment(shot, f"screenshot of {url}")

        html_text = ""
        if extract_html:
            html_text = _browser_extract_html(session, selector)

        header = f"[{session.get('title') or url}] {session.get('url') or url}"
        if shot:
            header += (f"\n[Screenshot saved to {shot} — attached to this "
                       "reply as an image]")
        out = f"{header}\n--- page text ---\n{text}"
        if extract_html:
            out += f"\n--- page html ---\n{html_text}"
        if action_results:
            out += _format_action_results(action_results)
        return out
    except Exception as exc:
        return f"[ERROR] Browse web failed: {exc}"
    finally:
        _browser_destroy(session)


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

# The sub-AI is selected by *intelligence level*, not by model id.  The routing
# table lives in core/models.py (SUBAGENT_TIERS); every model in it was verified
# live to accept a `tools` payload and answer with a tool call, so the sub-AI
# can always use the tools it is given.
#
# Output-budget handling — the delegate tool deliberately has no max_tokens
# argument any more, because a caller-chosen cap silently cuts a long answer
# in the middle.  Instead:
#   * a route whose provider is happy without the field does not send it
#     (Google, NVIDIA and Ollama were verified to answer until they are
#     done — finish_reason "stop" — when the field is missing),
#   * a route that needs the field sends the ceiling registered for it in
#     core/models.py (Groq silently truncates at 3072 tokens when the field
#     is missing; OpenRouter's credit check rejects requests without it),
#   * when OpenRouter answers "requires more credits, or fewer max_tokens",
#     the ceiling is halved and the same route is retried,
#   * and a truncated answer is never thrown away: the partial content is
#     carried into the next model with a "continue" instruction.
_SUBAGENT_REQUEST_TIMEOUT = 240.0     # seconds allowed for one provider call
_SUBAGENT_MIN_OUTPUT_BUDGET = 1024    # never shrink the ceiling below this
_SUBAGENT_MAX_BUDGET_HALVINGS = 4     # retries for a "budget too large" error
_SUBAGENT_MAX_TOOL_RESULT = 10000     # characters of a tool result kept in context

# The toolkit a delegated sub-AI gets when the caller does not name one.
#
# Advertising every tool of the system (73 schemas, ~46 KB of JSON) pushes
# several providers over their request-size limit before the sub-AI even reads
# the task — Groq answers "413 Request too large ... on tokens per minute" and
# OpenRouter charges for the schema on every round.  This core set covers the
# work a delegated subtask actually needs (files, shell, web, HTTP) and costs
# ~12 KB.  A caller that wants a wider set passes it explicitly through
# available_tools.
_SUBAGENT_DEFAULT_TOOLS: list[str] = [
    "shell", "read_file", "write_file", "find_replace",
    "list_directory", "search_files", "search_in_files", "index_files",
    "browse_web", "http_request", "get_datetime",
]


def _delegate_request_kwargs(entry: dict, messages: list[dict], budget: int | None) -> dict:
    """Build the provider-specific request payload for one sub-AI route."""
    pid = entry["provider_id"]
    kwargs: dict = {"model": entry["name"], "messages": messages}
    if budget:
        kwargs["max_tokens"] = budget
    if entry.get("reasoning"):
        if pid == "openrouter":
            # Reasoning runs but stays in the provider's own reasoning fields,
            # which is also what makes the cross-model reasoning carry work.
            kwargs["extra_body"] = {"reasoning": {"enabled": True}}
        elif pid == "nvidia":
            # NVIDIA gates the Nemotron/GLM reasoning models on this flag.
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": True}}
        elif pid in ("groq", "ollama", "ollama-local"):
            kwargs["reasoning_effort"] = "high"
    return kwargs


def _is_output_budget_error(exc: Exception) -> bool:
    """True when the provider refused the request because the output ceiling
    was too large for the account (OpenRouter's credit check)."""
    text = str(exc)
    return "402" in text and ("max_tokens" in text or "credit" in text.lower())


def _is_empty_balance_error(exc: Exception) -> bool:
    """True when the account has no credit left at all.

    OpenRouter's credit check answers "You requested up to N tokens, but your
    balance is only $0.00" in that case — no smaller budget will ever be
    accepted, so the route must be abandoned instead of burning one request
    per halving on it.
    """
    text = str(exc).lower()
    if "insufficient credits" in text:
        return True
    # "$0" / "$0.00" — but not "$0.02": a small balance can still be made to
    # fit by halving the requested output ceiling.
    return re.search(r"balance is only \$0(?:\.0+)?(?![.\d])", text) is not None


def _is_restricted_key_error(exc: Exception) -> bool:
    """True when *this key's* organization is blocked (not the whole provider).

    Groq answers "Organization has been restricted" when the organization that
    owns the key is restricted.  The restriction belongs to the key, not to the
    provider: probing all five configured Groq keys live on 2026-10-30 showed
    four of them restricted while the fifth answered normally.  Blocking the
    whole provider on this error would throw away the one key that works, so
    only the offending key is retired.
    """
    text = str(exc).lower()
    return "organization has been restricted" in text


def _next_free_route(rotation: list[dict], ind: int) -> int | None:
    """Index of the next route that works without paid credits.

    Used when a paid route is refused for lack of credit: walking through the
    remaining paid routes of the same provider would only repeat the refusal.
    """
    for offset in range(1, len(rotation)):
        candidate = rotation[(ind + offset) % len(rotation)]
        if candidate.get("free"):
            return (ind + offset) % len(rotation)
    return None


def _cap_sub_tool_result(text: str, limit: int = _SUBAGENT_MAX_TOOL_RESULT) -> str:
    """Shorten a tool result before it goes back into the sub-AI's context.

    A delegated worker has no context compressor, and a provider refuses a
    request that is too large ("Request too large for model ...").  The head
    and the tail of the result are kept: the head carries the beginning of
    the data, the tail usually carries the summary line the worker needs.
    """
    if not isinstance(text, str) or len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return (
        text[:head]
        + f"\n\n... [tool result truncated: {len(text) - limit} characters omitted] ...\n\n"
        + text[-tail:]
    )


def _dispatch_sub_tool(name: str, args_raw: str) -> str:
    """Helper to execute tools on behalf of a delegated sub-AI."""
    try:
        args = json.loads(args_raw)
    except Exception:
        args = {}

    local_funcs = _SUB_TOOL_FUNCS

    if name in local_funcs:
        try:
            res = local_funcs[name](**args)
            if not isinstance(res, str):
                res = json.dumps(res, indent=2)
            return res
        except Exception as e:
            return f"[TOOL ERROR] Execution failed: {str(e)}"

    return f"[TOOL ERROR] Tool '{name}' is not supported in the sub-AI delegation environment."


def _delegate_reasoning_text(reasoning, reasoning_details) -> str:
    """Flatten a sub-AI's reasoning into printable text.

    OpenRouter reports chain-of-thought as ``reasoning_details`` (a list of
    typed blocks) and leaves ``reasoning`` empty, so reading only the plain
    field hid the sub-AI's thinking completely — it reached the log but never
    reached the screen.  The main agent's display already handles both
    shapes; this is the same flattening for the delegation path.
    """
    if reasoning:
        text = str(reasoning).strip()
        if text:
            return text
    if isinstance(reasoning_details, list):
        parts = []
        for item in reasoning_details:
            if isinstance(item, dict):
                chunk = (item.get("text") or item.get("summary")
                         or item.get("content") or "")
                if chunk:
                    parts.append(str(chunk))
            elif item:
                parts.append(str(item))
        return "\n".join(parts).strip()
    if reasoning_details:
        return str(reasoning_details).strip()
    return ""


def _print_delegate_request(
    task: str,
    context: str,
    level: str,
    tools: list[str],
    iterations: int,
    system_prompt: str | None,
) -> None:
    """Print the manager's full delegation command.

    In the collapsed view only the one-line ``[DELEGATE]`` header is shown.
    In the expanded view the whole command is printed — the task, the context
    the manager supplied, the tools the sub-AI may use, the iteration budget
    and the system prompt — so nothing about the hand-over is hidden.
    """
    def _block(label: str, text: str) -> None:
        if not text:
            return
        print(f"{GRAY}[DELEGATE {label}]{RESET}")
        for line in str(text).splitlines():
            # A blank line stays blank — padding it would leave trailing
            # whitespace on the terminal.
            print(f"{GRAY}    {line}{RESET}" if line.strip() else "")

    print(f"{GRAY}[DELEGATE COMMAND] intelligence={level} "
          f"iterations={iterations}{RESET}")
    _block("TASK", task)
    _block("CONTEXT", context)
    if tools:
        print(f"{GRAY}[DELEGATE TOOLS] {', '.join(tools)}{RESET}")
    else:
        print(f"{GRAY}[DELEGATE TOOLS] (none — text-only sub-AI){RESET}")
    _block("SYSTEM PROMPT", system_prompt or "")


def delegate_subtask(
    task: str,
    context: str = "",
    intelligence: str = "medium",
    available_tools: list[str] | None = None,
    tool_call_iterations: int = 5,
    system_prompt: str | None = None,
) -> str:
    """Delegate a subtask to a sub-AI, routed by intelligence level.

    The caller picks an *intelligence level* ("high", "medium" or "low")
    instead of a model id; core/models.py maps that level onto a list of
    tool-calling capable models, ordered by capability.  The first model of
    the tier that answers wins; when a model fails the rotation moves on to
    the next one, and when the whole tier is exhausted it degrades to the
    tiers below it before it ever climbs back up.

    When an OpenRouter model produces reasoning (chain-of-thought) but the
    response is truncated or the call fails, the reasoning is carried to the
    next OpenRouter model via ``reasoning_details`` in the assistant message —
    the same pattern OpenRouter documents for multi-model reasoning continuity.

    The sub-AI can also use tools provided by the parent agent. When the sub-AI
    emits tool calls, they are executed locally via ``_dispatch_sub_tool`` and
    the results are fed back into the conversation for the next iteration.

    Args:
        task: Task description (imperative instruction).
        context: Optional background information.
        intelligence: "high", "medium" or "low" — which tier of sub-AI models
            runs the task.  Unknown values fall back to the default tier.
        available_tools: List of tool names the sub-AI is allowed to use.
            If None, the core delegation toolkit (``_SUBAGENT_DEFAULT_TOOLS``)
            is provided — the full tool set is far too large for several
            providers' request limits.  If empty list, no tools are provided
            (text-only).
        tool_call_iterations: Max tool-calling rounds (0 disables tools).
        system_prompt: Override for the sub-AI system prompt.
    """
    level = normalize_intelligence_level(intelligence)
    # The expanded/collapsed preference decides how much of the delegation
    # is shown on screen.  It is read once here so the whole delegation
    # (request, sub-AI tool calls, their results, the final report) is
    # rendered consistently.
    expanded = display_state.is_expanded()
    log_write(f"[delegate_subtask] intelligence:{level} task:{task[:80]}")
    print(f"{GRAY}[DELEGATE] → {level} | {task[:72]}{'...' if len(task) > 72 else ''}{RESET}")

    tool_call_iterations = max(0, int(tool_call_iterations))

    user_message = f"## Task\n{task.strip()}"
    if context and context.strip():
        user_message += f"\n\n## Context\n{context.strip()}"

    from llm_client import API_KEYS, _sanitize_messages_for_provider

    # Build the rotation: every model of the requested tier first, then the
    # tiers below it (degrade).  The ladder never climbs — a "medium" request
    # must not be served by a frontier model the caller did not ask for.  Each
    # model is tried with the provider's own keys before moving on.
    rotation: list[dict] = []
    for entry in get_subagent_rotation(level):
        pid = entry["provider_id"]
        base_url = PROVIDERS.get(pid, {}).get("base_url")
        if not base_url:
            continue
        for k in API_KEYS.get(pid, []):
            route = dict(entry)
            route.update({"key": k, "base_url": base_url, "pid": pid})
            rotation.append(route)

    if not rotation:
        return "[ERROR] No API keys configured for any sub-AI provider."

    ind = 0
    attempts = 0
    max_attempts = len(rotation)
    # Keys whose organization is blocked (e.g. Groq's "Organization has been
    # restricted").  The refusal belongs to the key, not to the provider, so
    # only that key is retired — the other keys of the same provider still get
    # their turn.
    blocked_keys: set[tuple[str, str]] = set()
    # Output ceiling learned per provider: when an account refuses a budget,
    # the next key of the same provider starts lower instead of rediscovering
    # the same refusal.
    provider_budget_ceiling: dict[str, int] = {}

    sys_p = system_prompt if system_prompt is not None else _DELEGATE_SYS_PROMPT

    # Persistent message list across fallback attempts so reasoning can carry
    # forward when switching between OpenRouter models.
    messages: list[dict] = [
        {"role": "system", "content": sys_p},
        {"role": "user",   "content": user_message},
    ]

    # Track reasoning from a prior attempt for cross-model carry within
    # OpenRouter.  Stored as a dict with provider/model/reasoning fields.
    carried_reasoning: dict | None = None

    # Determine which tools to expose to the sub-AI.
    # Only tools that _dispatch_sub_tool can actually execute are advertised —
    # otherwise the sub-AI is offered a tool whose dispatch would reject it.
    sub_tools = None
    sub_tool_names: set[str] = set()
    if tool_call_iterations > 0:
        if available_tools is None:
            sub_tool_names = set(_SUBAGENT_DEFAULT_TOOLS) & set(_SUB_TOOL_FUNCS)
        else:
            sub_tool_names = set(available_tools) & set(_SUB_TOOL_FUNCS)
        if sub_tool_names:
            sub_tools = [
                t for t in TOOLS_DESCRIPTION
                if t.get("function", {}).get("name") in sub_tool_names
            ]

    # The full command the manager AI handed over is always logged.  On
    # screen it is shown in full only when the details are expanded — the
    # collapsed view keeps the single header line printed above.
    granted_tools = sorted(sub_tool_names)
    runtime_log.log_delegate_request(
        task=task,
        context=context,
        intelligence=level,
        tools=granted_tools,
        iterations=tool_call_iterations,
        system_prompt=sys_p,
    )
    if expanded:
        _print_delegate_request(
            task, context, level, granted_tools, tool_call_iterations, sys_p
        )

    while attempts < max_attempts:
        cfg = rotation[ind]
        if (cfg["pid"], cfg["key"]) in blocked_keys:
            if all((r["pid"], r["key"]) in blocked_keys for r in rotation):
                break
            ind = (ind + 1) % len(rotation)
            continue
        headers = {"HTTP-Referer": "https://github.com/opsonusdh/Termux-AI", "X-Title": "Termux-AI"} if cfg["pid"] == "openrouter" else None
        client = OpenAI(
            api_key=cfg["key"],
            base_url=cfg["base_url"],
            default_headers=headers,
            timeout=_SUBAGENT_REQUEST_TIMEOUT,
            max_retries=0,
        )

        # The output ceiling registered for this route.  It is a ceiling, not
        # a target: it is only lowered when the provider says the account
        # cannot afford it.  A ceiling learned earlier in this delegation for
        # the same provider wins over the registered one.
        budget = cfg.get("max_tokens")
        learned = provider_budget_ceiling.get(cfg["pid"])
        if learned and (not budget or learned < budget):
            budget = learned
        halvings = 0

        # Inject carried reasoning from a prior OpenRouter model as a synthetic
        # assistant message so the new model can continue the chain-of-thought.
        if (
            carried_reasoning is not None
            and cfg["pid"] == "openrouter"
            and carried_reasoning.get("provider") == "openrouter"
            and carried_reasoning.get("model") != cfg["name"]
        ):
            carry_msg: dict = {
                "role": "assistant",
                "content": None,
                "_reasoning_provider": "openrouter",
                "_reasoning_model": carried_reasoning["model"],
            }
            if carried_reasoning.get("reasoning"):
                carry_msg["reasoning"] = carried_reasoning["reasoning"]
            if carried_reasoning.get("reasoning_details"):
                carry_msg["reasoning_details"] = carried_reasoning["reasoning_details"]
            if carry_msg not in messages:
                messages.append(carry_msg)
            messages.append({
                "role": "user",
                "content": (
                    "The previous model began reasoning but was cut off "
                    "before producing a final answer. Continue from that "
                    "reasoning and provide the complete final answer."
                ),
            })

        # Sanitize for the target provider, carrying reasoning across
        # OpenRouter models.
        same_model = (
            carried_reasoning is not None
            and carried_reasoning.get("model") == cfg["name"]
            and carried_reasoning.get("provider") == cfg["pid"]
        )
        clean_messages = _sanitize_messages_for_provider(
            messages,
            cfg["pid"],
            model_name=cfg["name"],
            preserve_native_reasoning=same_model,
            reasoning_carry_provider="openrouter" if cfg["pid"] == "openrouter" else None,
        )

        try:
            create_kwargs = _delegate_request_kwargs(cfg, clean_messages, budget)
            if sub_tools:
                create_kwargs["tools"] = sub_tools
                create_kwargs["tool_choice"] = "auto"

            # The account may not be able to afford the registered output
            # ceiling (OpenRouter checks the requested budget against the
            # balance).  Halve it and retry the very same route before giving
            # up on the model.
            while True:
                try:
                    resp = client.chat.completions.with_raw_response.create(**create_kwargs)
                    break
                except Exception as exc:
                    if not (
                        _is_output_budget_error(exc)
                        and budget
                        and halvings < _SUBAGENT_MAX_BUDGET_HALVINGS
                        and not _is_empty_balance_error(exc)
                    ):
                        raise
                    new_budget = max(_SUBAGENT_MIN_OUTPUT_BUDGET, budget // 2)
                    if new_budget >= budget:
                        raise
                    budget = new_budget
                    halvings += 1
                    # Remember what the account could not afford so the next
                    # key of this provider starts at the lower ceiling.
                    previous = provider_budget_ceiling.get(cfg["pid"])
                    if not previous or new_budget < previous:
                        provider_budget_ceiling[cfg["pid"]] = new_budget
                    create_kwargs = _delegate_request_kwargs(cfg, clean_messages, budget)
                    if sub_tools:
                        create_kwargs["tools"] = sub_tools
                        create_kwargs["tool_choice"] = "auto"
                    print(f"{GRAY}[DELEGATE] output ceiling too large for the account — retrying with {budget}{RESET}")

            response = resp.parse()
            raw_json = json.loads(resp.text)
            msg = response.choices[0].message
            content = msg.content

            # Extract reasoning for potential carry to the next model.
            reasoning = getattr(msg, "reasoning", None) or getattr(
                msg, "reasoning_content", None
            )
            reasoning_details = getattr(msg, "reasoning_details", None)
            if not reasoning_details:
                raw_message = (
                    raw_json.get("choices", [{}])[0].get("message", {})
                )
                reasoning_details = raw_message.get("reasoning_details")

            if reasoning or reasoning_details:
                carried_reasoning = {
                    "provider": cfg["pid"],
                    "model": cfg["name"],
                    "reasoning": reasoning,
                    "reasoning_details": reasoning_details,
                }
                # The sub-AI's thinking is logged the moment it arrives, so
                # a delegation that is cut short still shows what the
                # sub-AI was working on.
                reasoning_text = _delegate_reasoning_text(
                    reasoning, reasoning_details
                )
                runtime_log.log_reasoning(
                    cfg["pid"], cfg["name"], reasoning_text
                )
                # ``reasoning_details`` counts too: OpenRouter leaves the
                # plain ``reasoning`` field empty, and the expanded view used
                # to show nothing at all for those models.
                if expanded and reasoning_text:
                    print(f"{GRAY}[DELEGATE REASONING] "
                          f"{cfg['pid']}/{cfg['name']}{RESET}")
                    for line in reasoning_text.splitlines():
                        print(f"{GRAY}    {line}{RESET}")

            finish_reason = response.choices[0].finish_reason

            # ── TOOL-CALLING LOOP ────────────────────────────────────────────
            # If the sub-AI made tool calls, execute them via _dispatch_sub_tool
            # and feed results back for continued reasoning.
            subagent_tool_iterations = 0
            while msg.tool_calls and subagent_tool_iterations < tool_call_iterations:
                subagent_tool_iterations += 1

                # Build assistant message with tool_calls
                assistant_msg: dict = {
                    "role": "assistant",
                    "content": content or "",
                }
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                    } for tc in msg.tool_calls
                ]
                messages.append(assistant_msg)

                # Execute each tool call and collect results
                for tc in msg.tool_calls:
                    tool_name = tc.function.name
                    tool_args = tc.function.arguments
                    print(f"{GRAY}[DELEGATE TOOL] {tool_name}{RESET}")
                    # The sub-AI's tool call is logged with its arguments
                    # *before* it runs, and its result right after — the same
                    # real-time contract the main agent's tools follow.
                    runtime_log.log_delegate_tool_call(tool_name, tool_args)
                    if expanded:
                        args_text = runtime_log.format_args(tool_args)
                        if args_text:
                            for line in args_text.splitlines():
                                print(f"{GRAY}    {line}{RESET}")
                    t_res = _dispatch_sub_tool(tool_name, tool_args)
                    runtime_log.log_delegate_tool_result(tool_name, t_res)
                    if expanded:
                        print(f"{GRAY}[DELEGATE RESULT] {tool_name}{RESET}")
                        for line in str(t_res).splitlines():
                            print(f"{GRAY}    {line}{RESET}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": _cap_sub_tool_result(t_res),
                    })

                # Re-sanitize for provider and make next API call
                clean_messages = _sanitize_messages_for_provider(
                    messages,
                    cfg["pid"],
                    model_name=cfg["name"],
                    preserve_native_reasoning=same_model,
                    reasoning_carry_provider="openrouter" if cfg["pid"] == "openrouter" else None,
                )

                followup_kwargs = _delegate_request_kwargs(cfg, clean_messages, budget)
                if sub_tools:
                    followup_kwargs["tools"] = sub_tools
                    followup_kwargs["tool_choice"] = "auto"

                resp2 = client.chat.completions.with_raw_response.create(**followup_kwargs)
                response = resp2.parse()
                msg = response.choices[0].message
                content = msg.content
                finish_reason = response.choices[0].finish_reason
            # ── END TOOL-CALLING LOOP ────────────────────────────────────────

            if content and finish_reason != "length":
                runtime_log.log_delegate_report(
                    content.strip(), cfg["name"], str(cfg.get("tier", level))
                )
                header = (
                    f"[Sub-AI Report | intelligence={cfg.get('tier', level)} | "
                    f"model={cfg['name']} | "
                    f"task={task[:60].strip()}{'...' if len(task) > 60 else ''}]\n"
                    f"{'-' * 60}\n"
                )
                return header + content.strip()

            # Response was truncated or had no content but reasoning exists —
            # carry the reasoning and try the next model in the rotation.
            if not content or finish_reason == "length":
                if content:
                    messages.append({"role": "assistant", "content": content})
                if not carried_reasoning:
                    anchor = content[-80:].strip() if content else ""
                    if anchor:
                        messages.append({
                            "role": "user",
                            "content": (
                                "Continue exactly from where you left off. "
                                f"Do not repeat anything. Last words: '...{anchor}'"
                            ),
                        })
                    else:
                        messages.append({
                            "role": "user",
                            "content": "Continue.",
                        })

        except Exception as e:
            err = str(e).upper()
            is_transient = any(x in err for x in ("429", "RESOURCE_EXHAUSTED", "RATE LIMIT", "503", "UNAVAILABLE", "OVERLOADED"))
            if is_transient:
                print(f"{RED}[ERROR][{cfg['pid']}/{cfg['name']}] Rate-limited/overloaded. Trying next...{RESET}")
                time.sleep(2)
            else:
                print(f"{RED}[ERROR][{cfg['pid']}/{cfg['name']}] Error: {str(e)[:120]}{RESET}")

            # The organization behind *this key* is blocked.  The other keys
            # of the provider belong to other organizations and may still
            # work, so only this key is retired.
            if _is_restricted_key_error(e):
                blocked_keys.add((cfg["pid"], cfg["key"]))
                print(f"{RED}[ERROR][{cfg['pid']}] key organization blocked — skipping this key only{RESET}")

            # A paid route refused for lack of credit: skip ahead to a route
            # that works without paid credits instead of walking through every
            # other paid route of the same provider first.
            if _is_output_budget_error(e) and not cfg.get("free", True):
                nxt = _next_free_route(rotation, ind)
                if nxt is not None:
                    print(f"{GRAY}[DELEGATE] paid route refused for credit — switching to the free route {rotation[nxt]['provider_id']}/{rotation[nxt]['name']}{RESET}")
                    ind = nxt

            # When switching away from OpenRouter to a different provider,
            # reasoning carry from OpenRouter no longer applies.
            if cfg["pid"] != "openrouter" and carried_reasoning:
                carried_reasoning = None

            ind = (ind + 1) % len(rotation)
            attempts += 1
            continue

        # Truncated but no exception — move to the next model.
        ind = (ind + 1) % len(rotation)
        attempts += 1

    return (
        f"[ERROR] No sub-AI could serve the task at intelligence level '{level}' "
        f"after {attempts} attempt(s) across {len(rotation)} route(s)."
    )


# ── GENERATE IMAGE ─────────────────────────────────────────────────────────────

# Model IDs verified against the live /v1beta/openai/ model list. The old
# imagen-*-generate-* ids are not served by this API version (404) and used to
# hide the real error behind a bogus "model not found" message.
_NANO_BANANA_MODELS = [
    "gemini-3.1-flash-image-preview",
    "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image",
    "gemini-3-pro-image",
    "gemini-3.1-flash-lite-image",
    "gemini-2.5-flash-image",
]

_NANO_BANANA_QUALITY_ALIAS = {
    "flash": "gemini-3.1-flash-image-preview",
    "pro":   "gemini-3-pro-image-preview",
}

_NANO_BANANA_BASE_URL = PROVIDERS["google"]["base_url"]

_DATA_URI_RE = re.compile(r"data:image/\w+;base64,([A-Za-z0-9+/=]+)")


# ── NVIDIA IMAGE GENERATION (Visual Design endpoints) ──────────────────────────
#
# NVIDIA serves its FLUX image models from a *different* host than the chat
# endpoint in PROVIDERS["nvidia"]: image inference lives at
#
#     POST {base_url}/genai/{model}          base_url = ai.api.nvidia.com/v1
#
# The published NVIDIA reference pages disagree with the running service in
# several places, so every envelope below was verified against the live free
# endpoints rather than copied from the docs:
#
#   black-forest-labs/flux.3-image-klein  -> 404.  The live slug uses a
#       hyphen: "flux-3-image-klein".
#   black-forest-labs/flux-3-image-klein  -> OK.  `mode` accepts only the
#       literals "t2i"/"i2i" (not "Image Generation"/"Image Editing" as
#       documented); cfg_scale 1..20; steps 1..50; max 1,062,400 pixels.
#   black-forest-labs/flux.2-klein-4b     -> OK.  cfg_scale must be exactly 1;
#       steps 1..4; the endpoint rejects a "mode" key outright.
#   black-forest-labs/flux.1-dev          -> OK.  cfg_scale > 1 and <= 9;
#       steps 5..100; sending "mode" crashes the backend with a 500.
#   black-forest-labs/flux.1-schnell      -> consistently 504 (gateway
#       timeout after 300s) on the free endpoint — deliberately excluded.
#   black-forest-labs/flux.1-kontext-dev  -> image *editing* model; it needs
#       an input image and times out on a text-only request — out of scope
#       for a text-to-image tool.
#
# Response shape (identical for every model):
#   {"artifacts": [{"base64": "<JPEG bytes>", "finishReason": "SUCCESS",
#                   "seed": 123456}]}

_NVIDIA_IMAGE_PROVIDER_ID = "nvidia-images"

# Width/height values the NVIDIA image endpoints accept.
_NVIDIA_IMAGE_SIZE_ENUM = (768, 832, 896, 960, 1024, 1088, 1152, 1216, 1280, 1344)

# Quality tier -> preferred NVIDIA model.  "flash" is the 4-step distilled
# model; "pro" is the full FLUX.1-dev model.
_NVIDIA_IMAGE_QUALITY_ALIAS = {
    "flash": "black-forest-labs/flux.2-klein-4b",
    "pro":   "black-forest-labs/flux-3-image-klein",
}

# Request timeout for a single NVIDIA image call.  The healthy endpoints
# answer in 2-15s, so this is a generous ceiling for a cold start.  It is not
# a safety margin, it is a cost control: a hung endpoint (flux.1-dev has been
# observed timing out entirely) must not stall the whole rotation for
# minutes before the next model gets its turn.
_NVIDIA_IMAGE_TIMEOUT = 60

# ── NVIDIA endpoint health ───────────────────────────────────────────────────
# A hung endpoint must not eat the whole request budget.  With several models
# in the rotation, one dead endpoint used to cost minutes before the next
# model got its turn.  An endpoint that fails at the transport level is
# therefore remembered and skipped for a while; the cooldown grows with the
# number of consecutive failures.
_NVIDIA_IMAGE_HEALTH: dict[str, dict] = {}
_NVIDIA_IMAGE_HEALTH_BASE_COOLDOWN = 300.0
_NVIDIA_IMAGE_HEALTH_MAX_COOLDOWN = 1800.0


def _nvidia_image_is_healthy(model_name: str) -> bool:
    """False when the endpoint failed recently enough to be worth skipping."""
    entry = _NVIDIA_IMAGE_HEALTH.get(model_name)
    if not entry:
        return True
    cooldown = min(
        _NVIDIA_IMAGE_HEALTH_BASE_COOLDOWN * max(1, entry.get("fails", 1)),
        _NVIDIA_IMAGE_HEALTH_MAX_COOLDOWN,
    )
    return (time.time() - entry.get("last_fail", 0)) >= cooldown


def _nvidia_image_mark_failure(model_name: str) -> None:
    """Remember that an endpoint just failed at the transport level."""
    entry = _NVIDIA_IMAGE_HEALTH.setdefault(model_name, {"fails": 0})
    entry["fails"] = entry.get("fails", 0) + 1
    entry["last_fail"] = time.time()


def _nvidia_image_mark_success(model_name: str) -> None:
    """Forget a previous failure — the endpoint answered again."""
    _NVIDIA_IMAGE_HEALTH.pop(model_name, None)


def _nvidia_image_slots() -> list[dict]:
    """NVIDIA image-model slots straight from the canonical registry."""
    return [s for s in IMAGE_MODEL_SLOTS
            if s.get("provider_id") == _NVIDIA_IMAGE_PROVIDER_ID]


def _image_slot_by_name(model_name: str) -> dict | None:
    """Resolve a model id (full or bare slug) to its registry slot."""
    if not model_name:
        return None
    wanted = model_name.strip()
    bare = wanted.rsplit("/", 1)[-1]
    for slot in IMAGE_MODEL_SLOTS:
        if slot["name"] == wanted or slot["name"].rsplit("/", 1)[-1] == bare:
            return slot
    return None


def _snap_nvidia_dim(value, default: int = 1024, enum=None) -> int:
    """Snap an arbitrary pixel value onto the model's supported size grid.

    The grid is per model: FLUX.1 accepts 768..1344 in steps of 64, while
    FLUX.3 and FLUX.2 Klein accept 512..1568 in steps of 16 and Kontext has
    its own set of seventeen literals.
    """
    grid = tuple(enum) if enum else _NVIDIA_IMAGE_SIZE_ENUM
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = default
    return min(grid, key=lambda s: (abs(s - v), s))


def _nvidia_image_size(width, height, max_pixels: int, enum=None) -> tuple[int, int]:
    """Pick the largest supported size that fits the model's pixel budget.

    The NVIDIA image endpoints reject any request whose width*height exceeds
    1,062,400 pixels ("Requested image size N exceeds supported image size
    1062400"), so a requested size has to be snapped *and* budgeted.
    """
    grid = tuple(enum) if enum else _NVIDIA_IMAGE_SIZE_ENUM
    w_req = _snap_nvidia_dim(width, 1024, grid)
    h_req = _snap_nvidia_dim(height, 1024, grid)
    target_ratio = w_req / float(h_req)

    candidates = [
        (w, h) for w in grid for h in grid
        if not max_pixels or (w * h) <= max_pixels
    ]
    if not candidates:
        return 1024, 1024

    best = min(candidates, key=lambda wh: (
        abs((wh[0] / wh[1]) - target_ratio),
        abs((wh[0] * wh[1]) - (w_req * h_req)),
    ))
    return best


def _clamp_nvidia_cfg_scale(value, spec: dict) -> float:
    """Clamp cfg_scale into the model's accepted range."""
    default = float(spec.get("default", 1.0))
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    lo = float(spec.get("min", v))
    hi = float(spec.get("max", v))
    if spec.get("exclusive_min"):
        # e.g. flux.1-dev requires cfg_scale strictly greater than 1.
        lo = lo + 1e-3
        if v <= lo:
            v = lo
    elif v < lo:
        v = lo
    if v > hi:
        v = hi
    return v


def _clamp_nvidia_steps(value, spec: dict) -> int:
    """Clamp the diffusion step count into the model's accepted range."""
    default = int(spec.get("default", 1))
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    lo = int(spec.get("min", v))
    hi = int(spec.get("max", v))
    return max(lo, min(hi, v))


def _clamp_nvidia_seed(value) -> int:
    """Normalise a seed into a non-negative integer (0 = random)."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        pass
    try:
        return max(0, int(float(str(value).strip())))
    except (TypeError, ValueError):
        return 0


def _build_nvidia_image_body(
    slot: dict,
    prompt: str,
    width=None,
    height=None,
    seed=None,
    steps=None,
    cfg_scale=None,
    negative_prompt=None,
) -> dict:
    """Build a request body that satisfies the model's live parameter envelope."""
    max_px = int(slot.get("size_max_pixels", 0) or 0)
    w, h = _nvidia_image_size(width, height, max_px, slot.get("size_enum"))

    prompt_max = int(slot.get("prompt_max_len", 10000) or 10000)
    text = (prompt or "")[:prompt_max]

    body: dict = {
        "prompt": text,
        "width": w,
        "height": h,
        "cfg_scale": _clamp_nvidia_cfg_scale(cfg_scale, slot.get("cfg_scale") or {}),
        "steps": _clamp_nvidia_steps(steps, slot.get("steps") or {}),
        "samples": 1,
        "seed": _clamp_nvidia_seed(seed),
    }
    # `negative_prompt` is only accepted by some of the models — sending it
    # to flux.2-klein-4b is answered with an explicit "not supported" error.
    if negative_prompt and slot.get("supports_negative_prompt"):
        body["negative_prompt"] = str(negative_prompt)[:1000]

    # NOTE: `mode` is deliberately never sent.
    #   flux.2-klein-4b rejects it as an extra field (422),
    #   flux.1-kontext-dev rejects it as an extra field (422),
    #   and flux-3-image-klein defaults to text-to-image anyway.
    # The accepted literals per model are recorded in the registry for
    # reference: flux-3 't2i'|'i2i', flux.1-dev 'base'|'canny'|'depth',
    # flux.1-schnell 'base'.
    return body


def _extract_nvidia_artifacts(payload) -> tuple[bytes | None, str, int | None]:
    """Pull the first successful image out of an NVIDIA artifacts response.

    Returns ``(image_bytes, finish_reason, seed)``.  ``image_bytes`` is None
    when the payload carried no usable image.
    """
    if not isinstance(payload, dict):
        return None, "MALFORMED_RESPONSE", None

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return None, "NO_ARTIFACTS", None

    reasons: list[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        reason = str(artifact.get("finishReason") or "UNKNOWN")
        reasons.append(reason)
        if reason != "SUCCESS":
            continue
        encoded = artifact.get("base64") or ""
        if not encoded:
            continue
        try:
            raw = base64.b64decode(encoded)
        except Exception:
            continue
        if not raw:
            continue
        seed = artifact.get("seed")
        return raw, reason, seed if isinstance(seed, int) else None

    return None, (reasons[0] if reasons else "NO_ARTIFACTS"), None


# Magic-byte signatures for the image formats the providers actually return.
# NVIDIA's FLUX endpoints hand back JPEG while Gemini hands back PNG, so the
# file extension has to follow the bytes instead of a hardcoded guess —
# otherwise Android receives a ".png" name for JPEG data and opens it with the
# wrong MIME type (the share sheet and Gallery then show a broken file).
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
)


def _detect_image_extension(data: bytes) -> str:
    """Return the file extension that matches the image's actual format."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        return ".png"
    data = bytes(data)
    for signature, ext in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def _write_image_file(out_base: str, data: bytes) -> str:
    """Write image bytes next to ``out_base`` using a matching extension.

    ``out_base`` is a path *without* an extension — the real file name is
    decided from the bytes themselves, so JPEG output is never stored as
    ".png".  Returns the path of the file that was written.
    """
    out_path = f"{out_base}{_detect_image_extension(data)}"
    with open(out_path, "wb") as f:
        f.write(data)
    return out_path


def _nvidia_generate_image(
    prompt: str,
    slots: list[dict],
    out_base: str,
    width=None,
    height=None,
    seed=None,
    steps=None,
    cfg_scale=None,
    negative_prompt=None,
    respect_health: bool = False,
) -> tuple[str | None, list[str]]:
    """Try the NVIDIA FLUX endpoints in order.

    ``respect_health`` makes the rotation skip an endpoint that recently
    failed, so a dead model does not cost the full timeout on every call.
    It is off by default: a caller that hands over an explicit list of
    models — a pinned model, or a test — wants those models tried.

    Returns ``(success_message, errors)`` — exactly one of the two is set.
    """
    from llm_client import API_KEYS

    nvidia_keys = API_KEYS.get("nvidia", [])
    if not nvidia_keys:
        return None, ["nvidia: no NVIDIA API key found in api.keys."]

    base_url = PROVIDERS[_NVIDIA_IMAGE_PROVIDER_ID]["base_url"].rstrip("/")
    errors: list[str] = []
    filtered = 0
    # How many models were actually *tried*.  Models that were skipped
    # because they are self-hosted or need an input image do not count:
    # they never saw the prompt.
    attempted_models = 0

    for slot in slots:
        model_name = slot["name"]

        # A model NVIDIA lists in its catalogue but does not serve from the
        # hosted free API would only burn a request on a 404.
        if not slot.get("hosted_available", True):
            errors.append(
                f"{model_name}: {slot.get('hosted_note', 'not served by the hosted API')}"
            )
            continue

        # The image-editing models want an NVIDIA asset id, which the hosted
        # API will not accept as a local file or a URL.
        if slot.get("requires_image"):
            errors.append(
                f"{model_name}: image-editing model — it needs an input image "
                "supplied as an NVIDIA asset id, which the hosted API does "
                "not accept from a local file."
            )
            continue

        # An endpoint that just timed out is very likely still down; skip it
        # instead of paying the timeout again on the next call.  Only the
        # automatic rotation skips — a pinned model is always tried.
        if respect_health and not _nvidia_image_is_healthy(model_name):
            errors.append(
                f"{model_name}: skipped, the endpoint failed recently"
            )
            continue

        body = _build_nvidia_image_body(
            slot, prompt, width=width, height=height,
            seed=seed, steps=steps, cfg_scale=cfg_scale,
            negative_prompt=negative_prompt,
        )
        url = f"{base_url}/genai/{model_name}"

        for key in nvidia_keys:
            headers = {
                "Authorization": f"Bearer {key}",
                "accept": "application/json",
                "content-type": "application/json",
            }
            attempted_models += 1
            try:
                resp = requests.post(url, headers=headers, json=body,
                                     timeout=_NVIDIA_IMAGE_TIMEOUT)
            except Exception as exc:
                errors.append(f"{model_name}: request failed: {str(exc)[:200]}")
                # A transport failure (read timeout, connection reset) is a
                # property of the *endpoint*, not of the key — a bad key
                # comes back as an HTTP 401/403/429, never as a socket hang.
                # Retrying the next key would only repeat the same stall, so
                # move straight on to the next model.
                _nvidia_image_mark_failure(model_name)
                break

            if resp.status_code != 200:
                detail = resp.text[:200].replace("\n", " ").strip()
                errors.append(
                    f"{model_name}: HTTP {resp.status_code}"
                    f"{': ' + detail if detail else ''}"
                )
                if resp.status_code >= 500:
                    _nvidia_image_mark_failure(model_name)
                continue

            try:
                payload = resp.json()
            except Exception:
                errors.append(f"{model_name}: response was not valid JSON")
                continue

            img_bytes, reason, used_seed = _extract_nvidia_artifacts(payload)
            if img_bytes:
                out_path = _write_image_file(out_base, img_bytes)
                seed_note = f" (seed {used_seed})" if used_seed is not None else ""
                _nvidia_image_mark_success(model_name)
                return (
                    f"Successfully generated image and saved to {out_path} "
                    f"[nvidia/{model_name}{seed_note}]",
                    errors,
                )

            errors.append(f"{model_name}: no image returned (finishReason={reason})")
            if reason == "CONTENT_FILTERED":
                filtered += 1

    # Every model that was actually tried rejected the prompt on content
    # grounds — say so plainly.  Without this the user sees a wall of
    # near-identical per-model lines and has no way to tell that the
    # *prompt* is the problem, not the service.
    if filtered and attempted_models and filtered == attempted_models:
        return None, [
            "NVIDIA's safety filter rejected this prompt "
            "(finishReason=CONTENT_FILTERED) on every model tried. "
            "Rephrase it — some subjects (for example translucent or "
            "glowing organic bodies) trip the filter."
        ] + errors

    return None, errors


def _extract_image_bytes(response) -> bytes | None:
    """Pull inline image bytes out of an OpenAI-compatible image response.

    Gemini's OpenAI-compatible endpoint returns image-model output either as a
    data URI inside the text content or in ``message.images[].image_url.url``.
    Both shapes are handled here.
    """
    try:
        message = response.choices[0].message
    except Exception:
        return None

    content = getattr(message, "content", None) or ""
    match = _DATA_URI_RE.search(content)
    if match:
        try:
            return base64.b64decode(match.group(1))
        except Exception:
            return None

    images = getattr(message, "images", None)
    if images is None:
        extra = getattr(message, "model_extra", None) or {}
        if isinstance(extra, dict):
            images = extra.get("images")
    for image in (images or []):
        url = getattr(getattr(image, "image_url", None), "url", "") or ""
        match = _DATA_URI_RE.search(url)
        if match:
            try:
                return base64.b64decode(match.group(1))
            except Exception:
                return None
    return None


def _google_generate_image(
    prompt: str,
    model_order: list[str],
    out_base: str,
) -> tuple[str | None, list[str]]:
    """Try the Google Nano Banana models in order.

    Returns ``(success_message, errors)`` — exactly one of the two is set.
    """
    from llm_client import API_KEYS

    google_keys = API_KEYS.get("google", [])
    if not google_keys:
        return None, ["google: no Google API key found in api.keys."]

    # Base URLs come from core/models.py PROVIDERS — the single source of
    # truth.  Resolved at call time so an endpoint fix in the provider table
    # propagates without a restart.
    base_url = PROVIDERS["google"]["base_url"]

    errors: list[str] = []
    for model_name in model_order:
        for key in google_keys:
            client = OpenAI(api_key=key, base_url=base_url)
            try:
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                )

                img_bytes = _extract_image_bytes(response)
                if img_bytes:
                    out_path = _write_image_file(out_base, img_bytes)
                    return (
                        f"Successfully generated image and saved to {out_path} "
                        f"[google/{model_name}]",
                        errors,
                    )

                errors.append(
                    f"{model_name}: response contained no inline image data"
                )
            except Exception as e:
                errors.append(f"{model_name}: {str(e)[:200]}")

    return None, errors


def _attach_generated_image(message: str) -> str:
    """Queue a freshly generated image so the model can look at it.

    The tool result is a string, so the picture itself is handed over
    through the attachment queue that core/llm_client.py drains after every
    tool call.
    """
    marker = "saved to "
    idx = message.find(marker)
    if idx == -1:
        return message
    path = message[idx + len(marker):].split()[0].strip()
    queue_image_attachment(path, "generated image")
    return message


def generate_image(
    prompt: str,
    quality: str = "flash",
    filename: str | None = None,
    provider: str = "auto",
    model: str | None = None,
    width: int | None = None,
    height: int | None = None,
    seed: int | None = None,
    steps: int | None = None,
    cfg_scale: float | None = None,
    negative_prompt: str | None = None,
) -> str:
    """Generate an image from a text prompt.

    Providers:
      * ``google`` — Gemini native image generation ("Nano Banana").  The image
        comes back inline in the OpenAI-compatible chat response.
      * ``nvidia`` — NVIDIA's free FLUX endpoints on the Visual Design API.
        The image comes back as ``artifacts[].base64`` (JPEG).
      * ``auto``   — Google first, NVIDIA as a fallback.

    An explicit ``model`` pins the call to a single image model and implies
    its provider, e.g. ``black-forest-labs/flux.2-klein-4b``.

    The finished picture is queued as an attachment, so a multimodal model
    can look at what it just generated and judge the result.
    """
    prompt = (prompt or "").strip()
    quality = (quality or "flash").strip().lower()
    provider = (provider or "auto").strip().lower()

    log_write(
        f"[generate_image] provider:{provider} quality:{quality} "
        f"model:{model} filename:{filename} prompt:{prompt[:80]}"
    )
    print(f"{GRAY}[IMAGE GEN] {provider}/{quality} | {prompt[:68]}"
          f"{'...' if len(prompt) > 68 else ''}{RESET}")

    if not prompt:
        return "[generate_image ERROR] Prompt must not be empty."

    if provider not in ("auto", "google", "nvidia"):
        return (f"[generate_image ERROR] Unknown provider '{provider}'. "
                "Use 'google', 'nvidia' or 'auto'.")

    images_dir = os.path.join(paths.WORKSPACE_DIR, "images")
    os.makedirs(images_dir, exist_ok=True)

    if not filename or not filename.strip():
        filename = "image_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.splitext(filename.strip())[0]
    # The extension is decided from the returned bytes (see
    # _write_image_file), so the output path is built without one here.
    out_base = os.path.join(images_dir, filename)

    # ── Resolve which model(s) to try ──────────────────────────────────────
    google_order: list[str] = []
    nvidia_slots: list[dict] = []
    pinned_slot = _image_slot_by_name(model) if model else None

    if pinned_slot is not None:
        if pinned_slot.get("api") == "nvidia-genai":
            nvidia_slots = [pinned_slot]
        else:
            google_order = [pinned_slot["name"]]
    else:
        if model:
            return (f"[generate_image ERROR] Unknown image model '{model}'. "
                    "Known models: "
                    + ", ".join(s["name"] for s in IMAGE_MODEL_SLOTS))

        preferred = _NANO_BANANA_QUALITY_ALIAS.get(quality)
        google_order = _NANO_BANANA_MODELS.copy()
        if preferred and preferred in google_order:
            google_order.remove(preferred)
            google_order.insert(0, preferred)

        nvidia_slots = _nvidia_image_slots()
        nvidia_preferred = _NVIDIA_IMAGE_QUALITY_ALIAS.get(quality)
        if nvidia_preferred:
            nvidia_slots = [s for s in nvidia_slots if s["name"] == nvidia_preferred] \
                + [s for s in nvidia_slots if s["name"] != nvidia_preferred]

    # ── Run the rotation ───────────────────────────────────────────────────
    # An explicitly pinned model overrides the provider filter — asking for
    # "flux.2-klein-4b" with provider="google" is a contradiction, and the
    # pinned model is the more specific instruction.
    run_google = bool(google_order) and (pinned_slot is not None or provider in ("auto", "google"))
    run_nvidia = bool(nvidia_slots) and (pinned_slot is not None or provider in ("auto", "nvidia"))

    errors: list[str] = []
    attempted = False

    if run_google:
        attempted = True
        message, google_errors = _google_generate_image(
            prompt, google_order, out_base)
        errors.extend(google_errors)
        if message:
            _attach_generated_image(message)
            return message

    if run_nvidia:
        attempted = True
        message, nvidia_errors = _nvidia_generate_image(
            prompt, nvidia_slots, out_base,
            width=width, height=height, seed=seed,
            steps=steps, cfg_scale=cfg_scale,
            negative_prompt=negative_prompt,
            # The automatic rotation skips an endpoint that just failed; a
            # pinned model is always tried, because the caller asked for it
            # by name and it may have recovered.
            respect_health=(pinned_slot is None),
        )
        errors.extend(nvidia_errors)
        if message:
            _attach_generated_image(message)
            return message

    if not attempted:
        return "[generate_image ERROR] No image model was available to try."

    if not errors:
        return "[generate_image ERROR] Failed to generate image: no model was attempted."

    # Report every attempt, not just the last one — a single misleading
    # error (e.g. a 404 for a stale model id) used to hide the real cause.
    return ("[generate_image ERROR] Failed to generate image. Attempts:\n"
            + "\n".join(f"  - {e}" for e in errors))


# ── NEW UTILITY TOOLS ──────────────────────────────────────────────────────────

def list_directory(path: str, recursive: bool = False, max_depth: int = 3) -> str:
    """List files and directories at a given path."""
    log_write(f"[list_directory] path:{path} recursive:{recursive} max_depth:{max_depth}")
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(paths.ROOT, p)

    # Permission check for directory read
    approved, reason = request_file_permission("read", p)
    if not approved:
        return reason

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
                    entries.append("  ... (truncated at 200 entries)")
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

    # Permission check for both source and destination
    approved, reason = request_file_permission("write", op)
    if not approved:
        return reason
    approved, reason = request_file_permission("write", np)
    if not approved:
        return reason

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

    # Permission check for delete (write operation)
    approved, reason = request_file_permission("write", p)
    if not approved:
        return reason

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

    # Permission check for directory read
    approved, reason = request_file_permission("read", p)
    if not approved:
        return reason

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

def shell(bash: str, timeout: int = 30, total_timeout: int | None = None) -> str:
    """Execute shell commands in system environment after permission validation.

    ``timeout`` is applied **per stage**: every command of an ``&&``/``||``/
    ``;``/``|`` chain gets its own budget, so a slow first stage no longer
    starves the rest of the chain.  When a stage runs out of time, GNU
    ``timeout`` terminates that stage's whole process group.

    ``total_timeout`` is the **chain-wide** ceiling: it bounds the whole
    command no matter how many stages it has.  When it is not given, the
    ceiling is derived from the per-stage budget (one budget per stage, plus a
    small grace period) and capped, so a long chain can never block the chat
    loop indefinitely.

    When the chain-wide ceiling is reached, every descendant of the command is
    killed — the old implementation only killed the immediate ``bash`` process,
    which left orphaned children running.
    """
    log_write(f"[shell] timeout:{timeout} total:{total_timeout} cmd:{bash}")

    allowed, reason = validate_command(bash)
    if not allowed:
        return f"[PERMISSION DENIED] Action blocked by security policy. Reason: {reason}"

    try:
        result = shell_runner.run(
            bash, timeout=timeout, cwd=AI_ROOT, total_timeout=total_timeout
        )
    except Exception as e:
        return f"[ERROR] Failed to execute command: {e}"

    log_write(
        f"[shell] returncode:{result.returncode} dur:{result.duration:.2f}s "
        f"stages:{result.stages} wrapped:{result.wrapped} killed:{result.killed} "
        f"budget:{result.budget}"
    )
    if result.timed_out:
        log_write(f"[shell] TIMEOUT after {result.duration:.1f}s cmd:{bash[:120]}")
    elif result.stage_timed_out:
        log_write(f"[shell] STAGE TIMEOUT after {result.duration:.1f}s cmd:{bash[:120]}")

    return shell_runner.describe(result, timeout)


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
    # The canonical default set lives in core/app_config.py.  This copy used
    # to lag behind the interface's and never learned about the "notify"
    # flag, so a config.json created here was missing a key the rest of the
    # runtime expected.
    DEFAULT_CONFIG = dict(app_config.DEFAULT_CONFIG)
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
    except Exception:
        config = DEFAULT_CONFIG
    if not isinstance(config, dict):
        config = DEFAULT_CONFIG
    STT_PATH = os.path.expanduser(config.get("stt_path", DEFAULT_CONFIG["stt_path"]))

    try:
        # Add Termux-STT to path for the import
        if STT_PATH not in sys.path:
            sys.path.insert(0, STT_PATH)
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


def _check_battery(quiet: bool = False) -> dict:
    """Battery state.  ``quiet`` silences the wrapper's command trace."""
    try:
        if sys.platform == "win32":
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-CimInstance Win32_Battery | Select-Object EstimatedChargeRemaining, BatteryStatus"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            return {"raw": res.stdout.strip()}
        else:
            try:
                # The module-level name is the wrapper itself (loaded by path
                # at import time).  Importing "tools.wrapper_…" here depends
                # on the sys.path order — the "tools" name is shadowed by this
                # very module in some launch paths — and then fell through to
                # the raw subprocess call without saying so.
                status = get_battery_status(quiet=quiet)
                if isinstance(status, dict):
                    return status
            except Exception as e:
                # The wrapper already spent its whole command budget on
                # ``termux-battery-status``; the fallback below runs the very
                # same binary, so retrying it would only double the wait
                # (the battery broadcast is slow enough to time out on its
                # own).  Report the timeout instead.
                if "timed out" in str(e).lower():
                    return {"error": str(e)}
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


def _check_weather(quiet: bool = False) -> dict:
    """Weather forecast.  ``quiet`` is accepted for uniformity."""
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


def _check_storage(quiet: bool = False) -> dict:
    """Disk usage.  ``quiet`` is accepted for uniformity."""
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


def _check_memory(quiet: bool = False) -> dict:
    """RAM usage.  ``quiet`` is accepted for uniformity."""
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


def _check_network(quiet: bool = False) -> dict:
    """Network state.  ``quiet`` silences the wrapper's command trace."""
    try:
        if sys.platform == "win32":
            cmd = ["netsh", "wlan", "show", "interfaces"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            return {"raw": res.stdout.strip()[:500]}
        else:
            # The connected access point first.  ``termux-wifi-connectioninfo``
            # describes the link the phone is actually on and answers in a
            # fraction of the time a full scan takes; a scan on a busy radio
            # costs seconds and can hang outright when the Wi-Fi radio is
            # off.  It also reports the truth — whether the phone is
            # connected at all — where a scan only lists what is nearby.
            try:
                result = subprocess.run(
                    ["termux-wifi-connectioninfo"],
                    capture_output=True, text=True, timeout=10
                )
                data = json.loads(result.stdout)
                ssid = data.get("ssid")
                connected = bool(ssid) and ssid != "<unknown ssid>"
                return {
                    "ssid":           ssid,
                    "connected":      connected,
                    "state":          data.get("supplicant_state"),
                    "link_speed_mbps": data.get("link_speed_mbps"),
                    "rssi_dbm":       data.get("rssi"),
                    "ip":             data.get("ip"),
                    "source":         "connection-info",
                }
            except Exception as e:
                # A timed-out query has already spent the command budget;
                # the scan below is slower still, so there is no point in
                # starting it — report the timeout instead.
                if "timed out" in str(e).lower():
                    return {"error": str(e)}
            # Fallback: a scan at least names the networks that are around.
            try:
                # Module-level re-export again — see _check_battery.
                networks = get_wifi_scan_info(quiet=quiet)
                if networks:
                    best = networks[0]
                    return {
                        "ssid":           best.get("ssid"),
                        "connected":      False,
                        "link_speed_mbps": "N/A",
                        "rssi_dbm":       best.get("rssi"),
                        "ip":             "N/A",
                        "source":         "wifi-scan",
                    }
            except Exception:
                pass
            return {"error": "no network information available"}
    except Exception as e:
        return {"error": str(e)}


def _check_datetime(quiet: bool = False) -> dict:
    """Clock state.  ``quiet`` is accepted for uniformity."""
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


def run_diagnosis(quiet: bool = False) -> dict:
    """Collect a snapshot of the device state.

    ``quiet=True`` keeps the platform wrappers behind the checks from
    printing their ``[EXECUTING]`` / ``[OUT]`` trace.  The startup probe
    uses it: that probe runs on a background thread while the chat prompt is
    already on screen, and a trace printed there lands in the middle of the
    user's line — in the raw-mode fallback input path it even staircases
    across the screen, because a bare "\\n" no longer returns the cursor to
    column 0.

    The snapshot is bounded twice over: every check carries its own command
    timeout, and the collector gives up after ``DIAGNOSIS_TIMEOUT`` (plus a
    short grace period).  A device command that wedges — a Wi-Fi scan with the
    radio off, a battery broadcast that never arrives — must never turn a
    diagnostic into a stall, and it must never turn into an exception either:
    the caller always gets a dict.
    """
    results = {}
    executor = ThreadPoolExecutor(max_workers=len(CHECKS))
    stuck = False
    try:
        futures = {executor.submit(fn, quiet): name for name, fn in CHECKS.items()}
        try:
            for future in as_completed(futures, timeout=DIAGNOSIS_TIMEOUT):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as e:
                    results[name] = {"error": str(e)}
        except TimeoutError:
            stuck = True
            # Cancel whatever has not started yet, then give the running
            # checks a brief moment to report before the snapshot goes out.
            for future in futures:
                future.cancel()
            try:
                for future in as_completed(futures, timeout=DIAGNOSIS_GRACE):
                    name = futures[future]
                    if name not in results:
                        results[name] = {"error": "timeout"}
            except TimeoutError:
                # The grace period ran out too.  The checks that are still
                # running are reported as timeouts instead of being waited
                # for: a wedged device command must not hold the caller
                # hostage, and this collector must never raise — the caller
                # always gets a dict.
                pass
            for future, name in futures.items():
                if name not in results:
                    results[name] = {"error": "timeout"}
    finally:
        # ``wait=False`` on the stuck path: the executor's context manager
        # would otherwise block until every wedged command returned, which is
        # exactly what the deadline above exists to prevent.
        executor.shutdown(wait=not stuck, cancel_futures=True)
    return results


def run_background_diagnosis() -> dict:
    """Ambient diagnostics for the startup probe — no terminal output.

    The result is handed to the model as a system message (see
    core/interface.py::_get_diag_history), so the command trace would be
    pure noise on the user's screen.
    """
    return run_diagnosis(quiet=True)


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

    # The pairing state answers the question the service state cannot:
    # "is the session linked, or does it still need a pairing code?"
    pairing_raw = whatsapp_manager.gateway_state().get("state", "UNKNOWN")
    pairing = {
        "READY": "LINKED — the session is paired with this device",
        "QR_REQUIRED": "NOT PAIRED — a pairing code is needed "
                       "(manage_plugin action='setup')",
        "UNKNOWN": "UNKNOWN — the gateway did not answer",
    }.get(pairing_raw, pairing_raw)

    out = (
        f"WhatsApp Service State: {state}\n"
        f"Pairing State: {pairing}\n"
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
        if c.get("isPinned"):  parts.append("[PIN]")
        if c.get("isMuted"):   parts.append("[MUTE]")
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
            by_contact[e.get("sender_id", "unknown")].append(e)

        report_lines = [f" WhatsApp Report — {len(entries)} total message(s) across {len(by_contact)} contact(s)\n"]
        report_lines.append("=" * 50)

        for contact_id, msgs in by_contact.items():
            contact_name = msgs[0].get("sender_name") or contact_id
            report_lines.append(f"\n {contact_name} ({contact_id})")
            report_lines.append(f"   {len(msgs)} message(s):")
            for m in msgs:
                ts = (m.get("timestamp") or "")[:16].replace("T", " ")
                direction = m.get("direction", "")
                text = m.get("message", "")
                arrow = "←" if direction == "RECEIVED" else "→"
                report_lines.append(f"   [{ts}] {arrow} [{direction}] {text}")

        report_lines.append("\n" + "=" * 50)

        if clear:
            with open(wa_log_file, "w", encoding="utf-8"):
                pass
            report_lines.append(" Log cleared.")

        return "\n".join(report_lines)

    except Exception as e:
        return f"[ERROR] Failed to read WhatsApp report: {e}"


# ── GOOGLE PLUGIN TOOLS (Gmail / Calendar / Drive) ────────────────────────────
#
# These tools are advertised to the model only while their plugin is enabled
# *and* connected (see core/plugin_manager.py).  Every function therefore
# assumes a valid token and only has to deal with API-level failures — a
# missing connection is caught earlier, in the dispatcher.

def _google_fail(exc) -> str:
    """Format an API failure for the model."""
    return f"[ERROR] {exc}"


def get_gmail_messages(query: str = "", max_results: int = 10,
                       label_ids: str = "") -> str:
    """List recent Gmail messages (optionally filtered by a Gmail query).

    Each entry shows sender, subject and date so a message can be picked by
    content, not just by ID.  Use read_gmail_message with a message id to
    read the full body.
    """
    log_write(f"[get_gmail_messages] query:{query} max:{max_results}")
    try:
        data = google_api.gmail_list_messages(query=query,
                                               max_results=max_results,
                                               label_ids=label_ids)
    except google_api.GoogleApiError as e:
        return _google_fail(e)

    messages = data.get("messages", [])
    if not messages:
        return (f"No Gmail messages matched"
                f"{' query ' + query if query else ''}.")

    lines = [f"Gmail messages ({len(messages)} of ~{data.get('estimate', len(messages))}):"]
    for msg in messages:
        lines.append(
            f"  - id={msg.get('id','')}  "
            f"from={msg.get('from','')!r}  "
            f"subject={msg.get('subject','')!r}  "
            f"date={msg.get('date','')!r}"
        )
        snippet = (msg.get("snippet") or "").strip()
        if snippet:
            lines.append(f"      {snippet}")
    lines.append("\nUse read_gmail_message with a message id to read the full content.")
    return "\n".join(lines)


def search_gmail(query: str, max_results: int = 20) -> str:
    """Search Gmail with Gmail's own search syntax.

    Each entry shows sender, subject and date.  Use read_gmail_message with a
    message id to read the full body.
    """
    log_write(f"[search_gmail] query:{query}")
    if not query:
        return "[ERROR] A search query is required."
    try:
        data = google_api.gmail_list_messages(query=query,
                                               max_results=max_results)
    except google_api.GoogleApiError as e:
        return _google_fail(e)

    messages = data.get("messages", [])
    if not messages:
        return f"No Gmail messages matched '{query}'."

    lines = [f"Gmail search '{query}' — {len(messages)} match(es):"]
    for msg in messages:
        lines.append(
            f"  - id={msg.get('id','')}  "
            f"from={msg.get('from','')!r}  "
            f"subject={msg.get('subject','')!r}  "
            f"date={msg.get('date','')!r}"
        )
        snippet = (msg.get("snippet") or "").strip()
        if snippet:
            lines.append(f"      {snippet}")
    return "\n".join(lines)


def get_gmail_labels() -> str:
    """List the Gmail labels of the connected account."""
    log_write("[get_gmail_labels]")
    try:
        data = google_api.gmail_list_labels()
    except google_api.GoogleApiError as e:
        return _google_fail(e)

    labels = data.get("labels", [])
    if not labels:
        return "No Gmail labels found."
    lines = [f"Gmail labels ({len(labels)}):"]
    for label in labels:
        lines.append(
            f"  - {label.get('name','')}  [id={label.get('id','')}"
            f" type={label.get('type','')}"
            f" total={label.get('messages_total',0)}"
            f" unread={label.get('unread',0)}]"
        )
    return "\n".join(lines)


def create_gmail_label(name: str) -> str:
    """Create a Gmail label."""
    log_write(f"[create_gmail_label] name:{name}")
    if not name:
        return "[ERROR] A label name is required."
    try:
        data = google_api.gmail_create_label(name)
    except google_api.GoogleApiError as e:
        return _google_fail(e)
    return f"Created Gmail label '{data.get('name', name)}' (id={data.get('id','')})."


def delete_gmail_message(message_id: str, permanent: bool = False) -> str:
    """Trash (or permanently delete) a Gmail message."""
    log_write(f"[delete_gmail_message] id:{message_id} permanent:{permanent}")
    if not message_id:
        return "[ERROR] A message id is required."
    try:
        data = google_api.gmail_delete_message(message_id, permanent=permanent)
    except google_api.GoogleApiError as e:
        return _google_fail(e)
    return f"Gmail message {message_id} {data.get('action', 'processed')}."


def send_gmail_message(to: str, subject: str, body: str,
                       cc: str = "", bcc: str = "") -> str:
    """Send an email through the connected Gmail account."""
    log_write(f"[send_gmail_message] to:{to} subject:{subject}")
    if not to:
        return "[ERROR] A recipient ('to') is required."
    try:
        data = google_api.gmail_send_message(to=to, subject=subject,
                                             body=body, cc=cc, bcc=bcc)
    except google_api.GoogleApiError as e:
        return _google_fail(e)
    return (f"Sent email to {to} (message id={data.get('id','')}, "
            f"thread id={data.get('thread_id','')}).")


def read_gmail_message(message_id: str, format: str = "full") -> str:
    """Read the full content of a single Gmail message by ID.

    ``format`` is ``full`` (headers + body + attachments) or ``metadata``
    (headers + snippet only).
    """
    log_write(f"[read_gmail_message] id:{message_id} format:{format}")
    if not message_id:
        return "[ERROR] A message id is required."
    try:
        if format == "metadata":
            data = google_api._gmail_message_meta(message_id)
            if not data:
                return f"[ERROR] Could not read message {message_id}."
            lines = [
                f"From: {data.get('from','')}",
                f"To: {data.get('to','')}",
                f"Subject: {data.get('subject','')}",
                f"Date: {data.get('date','')}",
                "",
                f"Snippet: {data.get('snippet','') or '(no snippet)'}",
            ]
            return "\n".join(lines)
        data = google_api.gmail_get_message(message_id)
    except google_api.GoogleApiError as e:
        return _google_fail(e)

    body = data.get("body") or ""
    lines = [
        f"From: {data.get('from','')}",
        f"To: {data.get('to','')}",
        f"Cc: {data.get('cc','')}",
        f"Subject: {data.get('subject','')}",
        f"Date: {data.get('date','')}",
        f"Labels: {', '.join(data.get('labels') or [])}",
        "",
        f"--- Body ({len(body)} chars) ---",
        body or "(no readable body)",
    ]
    if data.get("attachments"):
        lines.append("")
        lines.append("Attachments:")
        for att in data["attachments"]:
            lines.append(
                f"  - {att.get('filename','')}"
                f" ({att.get('mime_type','')}, {att.get('size',0)} bytes)"
            )
    return "\n".join(lines)


def get_gmail_profile() -> str:
    """Show the connected Gmail account's profile (address, message counts)."""
    log_write("[get_gmail_profile]")
    try:
        data = google_api.gmail_get_profile()
    except google_api.GoogleApiError as e:
        return _google_fail(e)
    email = data.get("email_address") or "(not available)"
    return (
        f"Connected Gmail account: {email}\n"
        f"Messages total: {data.get('messages_total', 0)}\n"
        f"Threads total:  {data.get('threads_total', 0)}\n"
        f"History id:     {data.get('history_id', '')}"
    )


def list_calendars() -> str:
    """List the Google Calendars the account can access."""
    log_write("[list_calendars]")
    try:
        data = google_api.calendar_list_calendars()
    except google_api.GoogleApiError as e:
        return _google_fail(e)

    calendars = data.get("calendars", [])
    if not calendars:
        return "No Google calendars found."
    lines = [f"Google calendars ({len(calendars)}):"]
    for cal in calendars:
        tag = " [primary]" if cal.get("primary") else ""
        lines.append(f"  - {cal.get('summary','')}{tag}  "
                     f"id={cal.get('id','')} role={cal.get('access_role','')}")
    return "\n".join(lines)


def get_calendar_events(calendar_id: str = "primary", time_min: str = "",
                        time_max: str = "", max_results: int = 25,
                        query: str = "") -> str:
    """List Google Calendar events.

    With no time bounds the window defaults to 30 days in the past through
    90 days ahead, so "today's events" show up without you having to pass
    timeMin/timeMax explicitly.
    """
    log_write(f"[get_calendar_events] cal:{calendar_id} query:{query}")
    try:
        data = google_api.calendar_list_events(calendar_id=calendar_id,
                                               time_min=time_min,
                                               time_max=time_max,
                                               max_results=max_results,
                                               query=query)
    except google_api.GoogleApiError as e:
        return _google_fail(e)

    events = data.get("events", [])
    if not events:
        hint = ""
        if not time_min and not time_max:
            hint = (" Try passing time_min/time_max (ISO-8601) to look in a "
                    "specific window.")
        return (f"No events found in calendar '{data.get('calendar_id', calendar_id)}'."
                f"{hint}")
    window = ""
    if data.get("time_min") and data.get("time_max"):
        window = f" (window {data['time_min']} → {data['time_max']})"
    lines = [f"Events in '{data.get('calendar_id', calendar_id)}' ({len(events)}):{window}"]
    for event in events:
        tag = ""
        status = event.get("status", "")
        if status == "cancelled":
            tag = " [CANCELLED]"
        elif event.get("all_day"):
            tag = " [all-day]"
        lines.append(
            f"  - [{event.get('start','')} → {event.get('end','')}]"
            f" {event.get('summary','')}{tag}  (id={event.get('id','')})"
        )
        if event.get("location"):
            lines.append(f"      at {event['location']}")
        if event.get("description"):
            lines.append(f"      {event['description'][:120]}")
        if event.get("attendees"):
            lines.append(f"      attendees: {', '.join(event['attendees'])}")
    nxt = data.get("next_page_token", "")
    if nxt:
        lines.append(f"\nMore pages available — pass page_token={nxt!r} to see the next page.")
    return "\n".join(lines)


def create_calendar_event(calendar_id: str = "primary", summary: str = "",
                           start: str = "", end: str = "",
                           description: str = "", location: str = "",
                           attendees: list | None = None) -> str:
    """Create a Google Calendar event."""
    log_write(f"[create_calendar_event] cal:{calendar_id} summary:{summary}")
    try:
        data = google_api.calendar_create_event(calendar_id=calendar_id,
                                                summary=summary, start=start,
                                                end=end, description=description,
                                                location=location,
                                                attendees=attendees)
    except google_api.GoogleApiError as e:
        return _google_fail(e)
    link = data.get("html_link", "")
    return (f"Created event '{data.get('summary', summary)}' "
            f"({data.get('start','')} → {data.get('end','')}, "
            f"id={data.get('id','')})."
            + (f"\n{link}" if link else ""))


def update_calendar_event(event_id: str, calendar_id: str = "primary",
                           summary: str = "", start: str = "", end: str = "",
                           description: str = "", location: str = "",
                           attendees: list | None = None) -> str:
    """Update a Google Calendar event."""
    log_write(f"[update_calendar_event] id:{event_id}")
    try:
        data = google_api.calendar_update_event(event_id=event_id,
                                                calendar_id=calendar_id,
                                                summary=summary, start=start,
                                                end=end, description=description,
                                                location=location,
                                                attendees=attendees)
    except google_api.GoogleApiError as e:
        return _google_fail(e)
    return (f"Updated event {data.get('id', event_id)}"
            + (f" — '{data.get('summary','')}'" if data.get("summary") else ""))


def delete_calendar_event(event_id: str, calendar_id: str = "primary") -> str:
    """Delete a Google Calendar event."""
    log_write(f"[delete_calendar_event] id:{event_id}")
    try:
        data = google_api.calendar_delete_event(event_id=event_id,
                                                calendar_id=calendar_id)
    except google_api.GoogleApiError as e:
        return _google_fail(e)
    return f"Deleted event {data.get('id', event_id)}."


def list_drive_files(query: str = "", page_size: int = 25,
                     order_by: str = "modifiedTime desc") -> str:
    """List Google Drive files."""
    log_write(f"[list_drive_files] query:{query}")
    try:
        data = google_api.drive_list_files(query=query,
                                            page_size=page_size,
                                            order_by=order_by)
    except google_api.GoogleApiError as e:
        return _google_fail(e)

    files = data.get("files", [])
    if not files:
        return "No Google Drive files matched."
    lines = [f"Google Drive files ({len(files)}):"]
    for item in files:
        size = item.get("size", "")
        size_txt = f"  {int(size) // 1024} KB" if str(size).isdigit() else ""
        lines.append(f"  - {item.get('name','')}  [id={item.get('id','')}"
                     f" type={item.get('mime_type','')}{size_txt}]")
    return "\n".join(lines)


def upload_drive_file(file_path: str, name: str = "",
                      parent_folder_id: str = "") -> str:
    """Upload a local file to Google Drive."""
    log_write(f"[upload_drive_file] path:{file_path}")
    try:
        data = google_api.drive_upload_file(file_path=file_path, name=name,
                                            parent_folder_id=parent_folder_id)
    except google_api.GoogleApiError as e:
        return _google_fail(e)
    link = data.get("link", "")
    return (f"Uploaded '{data.get('name','')}' to Google Drive "
            f"(id={data.get('id','')})."
            + (f"\n{link}" if link else ""))


def download_drive_file(file_id: str, output_path: str = "") -> str:
    """Download a Google Drive file to the device."""
    log_write(f"[download_drive_file] id:{file_id}")
    try:
        data = google_api.drive_download_file(file_id=file_id,
                                              output_path=output_path)
    except google_api.GoogleApiError as e:
        return _google_fail(e)
    return (f"Downloaded Drive file {file_id} to {data.get('path','')} "
            f"({data.get('size', 0)} bytes).")


def delete_drive_file(file_id: str) -> str:
    """Delete a Google Drive file."""
    log_write(f"[delete_drive_file] id:{file_id}")
    try:
        data = google_api.drive_delete_file(file_id=file_id)
    except google_api.GoogleApiError as e:
        return _google_fail(e)
    return f"Deleted Google Drive file {data.get('id', file_id)}."


def create_drive_folder(name: str, parent_folder_id: str = "") -> str:
    """Create a Google Drive folder."""
    log_write(f"[create_drive_folder] name:{name}")
    try:
        data = google_api.drive_create_folder(name=name,
                                              parent_folder_id=parent_folder_id)
    except google_api.GoogleApiError as e:
        return _google_fail(e)
    return (f"Created Drive folder '{data.get('name', name)}' "
            f"(id={data.get('id','')}).")


def share_drive_file(file_id: str, email: str, role: str = "reader",
                     permission_type: str = "user") -> str:
    """Share a Google Drive file with somebody."""
    log_write(f"[share_drive_file] id:{file_id} email:{email} role:{role}")
    try:
        data = google_api.drive_share_file(file_id=file_id, email=email,
                                            role=role,
                                            permission_type=permission_type)
    except google_api.GoogleApiError as e:
        return _google_fail(e)
    return (f"Shared Drive file {file_id} with {email} "
            f"(role={data.get('role', role)}, type={data.get('type', permission_type)}).")


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
            try:
                from core.models import MODEL_SLOTS, AGENT_MODEL_SLOTS, SUBAGENT_TIERS
                for slot in MODEL_SLOTS + AGENT_MODEL_SLOTS:
                    if slot.get("name") == m:
                        pid = slot["provider_id"]
                        return pid, PROVIDERS[pid]["base_url"]
                for entries in SUBAGENT_TIERS.values():
                    for entry in entries:
                        if entry.get("name") == m:
                            pid = entry["provider_id"]
                            return pid, PROVIDERS[pid]["base_url"]
            except Exception:
                pass

            # Base URLs come from core/models.py PROVIDERS — the single source
            # of truth.
            if m.startswith("openrouter") or "/" in m:
                pid = "openrouter"
            elif m.startswith("gemini") or m.startswith("gemma"):
                pid = "google"
            elif "llama" in m or "mixtral" in m or "qwen" in m or "gpt" in m:
                pid = "groq"
            elif "nvidia" in m or "nemotron" in m or "deepseek" in m:
                pid = "nvidia"
            else:
                pid = "google"
            return pid, PROVIDERS[pid]["base_url"]

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
    except Exception:
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
    
    # The runtime is about to block waiting for the answer.  Push a
    # notification so the user finds out even when the terminal is in the
    # background — with "notify" switched off this is a no-op.
    notifier.question(question, options)

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

    # The runtime is about to block waiting for the yes/no answer.  Push a
    # notification so the user finds out even when the terminal is in the
    # background — with "notify" switched off this is a no-op.
    notifier.confirmation(question, default)
    
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
        return f"[TODO] {title}\n{'─' * 40}\n  (no tasks yet)\n"
    total = len(tasks)
    done = sum(1 for t in tasks if t.get("done"))
    lines = [
        f"[TODO] {title}",
        f"   Progress: {done}/{total} completed ({round(done / total * 100)}%)",
        f"{'─' * 40}",
    ]
    for t in tasks:
        box = "[OK]" if t.get("done") else "[ ]"
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
        return "[CLEAR] All tasks cleared. The todo list is now empty."

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
        return f"[OK] Created new todo list '{new_title}' with {len(new_tasks)} task(s).\n" + _format_todos(data)

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
            return f"[OK] Checked task {task_id}: {target['text']}\n" + _format_todos(data)
        else:
            target["done"] = False
            _save_todos(data)
            return f"[ ] Unchecked task {task_id}: {target['text']}\n" + _format_todos(data)

    if action == "delete":
        if task_id is None:
            return "[ERROR] task_id is required for 'delete' action."
        data = _load_todos()
        target = next((t for t in data["tasks"] if t["id"] == task_id), None)
        if target is None:
            return f"[ERROR] No task found with ID {task_id}."
        data["tasks"] = [t for t in data["tasks"] if t["id"] != task_id]
        _save_todos(data)
        return f"[DEL] Deleted task {task_id}: {target['text']}\n" + _format_todos(data)

    return f"[ERROR] Unknown action '{action}'. Valid: create, add, list, check, uncheck, delete, clear."


# ── PLUGIN MANAGEMENT TOOL ────────────────────────────────────────────────────
#
# Lets the model answer "turn on Gmail" / "is Calendar connected?" without the
# user having to remember the slash command.
#
# Enabling an account plugin is only half the job: the Google account still has
# to grant access.  A tool call cannot sit and wait minutes for a browser click
# the way '/plugin <name> setup' does, so the consent flow is split here —
# 'setup' returns immediately with the URL to open, and 'finish' collects the
# result afterwards, with a bounded wait.
#
# Deliberately NOT added to _SUB_TOOL_FUNCS: enabling a plugin mutates shared
# configuration, which is a main-agent decision, not a delegated one.

def _plugin_state_line(plugin_name: str) -> str:
    """One readable line describing a plugin's enabled + connected state."""
    pm = plugin_manager_module.plugin_manager
    state = pm.plugin_state(plugin_name) or {}
    enabled = "enabled" if state.get("enabled") else "disabled"

    if not state.get("requires_setup"):
        return f"{plugin_name}: {enabled} (local — no account setup needed)"
    if state.get("authorized"):
        account = state.get("account") or ""
        return (f"{plugin_name}: {enabled}, connected"
                + (f" as {account}" if account else ""))
    if not state.get("client_configured"):
        return (f"{plugin_name}: {enabled}, NOT connected "
                f"(no Google OAuth client configured yet)")
    return f"{plugin_name}: {enabled}, NOT connected (access not granted yet)"


def _plugin_setup_prompt(plugin_name: str, service: str) -> str:
    """Start the consent flow and return the URL the user has to open."""
    info, error = google_oauth.begin_loopback_consent(service)
    if error:
        return (f"[ERROR] Could not start the Google consent flow for "
                f"'{plugin_name}': {error}\n"
                f"The OAuth client has to be an application of type 'Desktop app' "
                f"— create one at {google_oauth.CLOUD_CONSOLE_CLIENTS}.\n"
                f"Run '/plugin setup {plugin_name}' to do it interactively.")

    url = info.get("url") or ""
    lines = [f"'{plugin_name}' needs permission to access your Google account.",
             "",
             "Open this URL in the browser on this device and approve the request:"]
    lines.append(f"  {url}")
    lines.append("")
    lines.append("If you approve it on another device instead, copy the "
                 "address-bar URL (it contains ?code=...) and give it back to me.")
    lines.append("")
    lines.append("Tell me once you have approved it and I will finish connecting "
                 "the account.")
    return "\n".join(lines)


def manage_plugin(action: str = "list", plugin: str = "", code: str = "") -> str:
    """Inspect and control plugins: list, status, enable, disable, setup, finish, disconnect."""
    log_write(f"[manage_plugin] action:{action} plugin:{plugin}")
    pm = plugin_manager_module.plugin_manager

    action = (action or "list").strip().lower()
    plugin = (plugin or "").strip().lower()

    if action in ("list", ""):
        names = list(pm.list_plugins().keys())
        return ("Termux-AI plugins:\n"
                + "\n".join("  - " + _plugin_state_line(n) for n in names))

    if not plugin:
        return ("[ERROR] A plugin name is required for this action. Available: "
                + ", ".join(pm.list_plugins().keys()))

    state = pm.plugin_state(plugin)
    if not state:
        return (f"[ERROR] Unknown plugin '{plugin}'. Available: "
                + ", ".join(pm.list_plugins().keys()))

    if action == "status":
        desc = state.get("description") or ""
        lines = [f"Plugin '{plugin}': {_plugin_state_line(plugin)}"]
        if desc:
            lines.append(f"  {desc}")
        # An enabled plugin whose runtime is down still fails every call —
        # the model should see that before it tries.
        runtime_getter = getattr(pm, "runtime_state", None)
        if callable(runtime_getter):
            runtime = runtime_getter(plugin) or {}
            if runtime.get("state") not in ("no_runtime", "unknown"):
                mark = "running" if runtime.get("ok") else "NOT running"
                lines.append(f"  Runtime: {mark} — {runtime.get('message', '')}")
        return "\n".join(lines)

    if action == "disable":
        if not pm.is_enabled(plugin):
            return f"Plugin '{plugin}' is already disabled."
        deactivate = getattr(pm, "deactivate", None)
        if callable(deactivate):
            result = deactivate(plugin)
            runtime = result.get("runtime") or {}
            message = (f"Plugin '{plugin}' disabled. Its tools are no longer "
                       f"offered to me.")
            if runtime.get("message"):
                message += f" {runtime['message']}"
            return message
        pm.disable(plugin)
        return f"Plugin '{plugin}' disabled. Its tools are no longer offered to me."

    if action in ("enable", "setup", "connect"):
        if not state.get("requires_setup"):
            if action == "setup":
                # The explicit setup path — for WhatsApp this is the
                # pairing step.  The code itself is rendered in the user's
                # terminal by the runtime, never here and never on its own.
                setup = getattr(pm, "setup", None)
                if callable(setup):
                    result = setup(plugin)
                    message = result.get("message") or ""
                    if result.get("ok"):
                        if result.get("state") == "reset":
                            return (message + " The WhatsApp tools are no "
                                    "longer offered to me until the plugin "
                                    "is enabled again.")
                        return message
                    return f"[ERROR] {message}"
            # A local plugin: enabling it also starts whatever it needs to
            # run (the Termux-WP gateway for WhatsApp).  The tools are in my
            # tool list from the next request on, so I can carry on with the
            # task straight away instead of asking the user to repeat it.
            activate = getattr(pm, "activate", None)
            if callable(activate):
                result = activate(plugin)
                if not result.get("ok"):
                    return (f"[ERROR] Could not enable '{plugin}': "
                            f"{result.get('message', 'unknown error')}")
                runtime = result.get("runtime") or {}
                tools = result.get("tools") or []
                if runtime.get("ok"):
                    lines = [
                        (f"Plugin '{plugin}' enabled (local plugin — no account "
                         f"setup needed). {runtime.get('message', '')}").strip(),
                        f"Its {len(tools)} tools are now in my tool list — I can "
                        f"use them right away, so carry on with the task without "
                        f"asking the user to repeat it.",
                    ]
                    if plugin == "whatsapp":
                        lines.append("(If this device is not paired yet, call "
                                     "manage_plugin(action='setup', "
                                     "plugin='whatsapp') to put the pairing "
                                     "code on the user's screen — it is never "
                                     "printed on its own.)")
                    return "\n".join(lines)
                return (f"Plugin '{plugin}' is enabled, but its runtime did not "
                        f"start: {runtime.get('message', 'unknown error')}\n"
                        f"The {len(tools)} tools are advertised but will fail "
                        f"until the runtime is up.")
            if pm.is_enabled(plugin):
                return f"Plugin '{plugin}' is already enabled (no account setup needed)."
            pm.enable(plugin)
            return f"Plugin '{plugin}' enabled (local plugin — no account setup needed)."

        was_enabled = pm.is_enabled(plugin)
        if not was_enabled:
            pm.enable(plugin)
        prefix = "" if was_enabled else f"Plugin '{plugin}' enabled. "

        if state.get("authorized"):
            account = state.get("account") or ""
            return (prefix + f"'{plugin}' is already connected"
                    + (f" as {account}" if account else "") + ".")

        return prefix + _plugin_setup_prompt(plugin, state.get("service"))

    if action == "finish":
        if not state.get("requires_setup"):
            return f"Plugin '{plugin}' does not need a setup step."
        if state.get("authorized"):
            account = state.get("account") or ""
            return ("'" + plugin + "' is already connected"
                    + (f" as {account}" if account else "") + ".")

        service = state.get("service")

        # The user may have approved on another device and copied the
        # redirect URL — that is a complete code exchange on its own.
        if (code or "").strip():
            ok, detail = google_oauth.complete_code_flow(service, code)
            if ok:
                return (f"'{plugin}' is now connected"
                        + (f" as {detail}" if detail else "")
                        + ". Its tools are available.")
            return (f"[ERROR] '{plugin}' setup did not finish: {detail}")

        outcome = google_oauth.finish_loopback_consent(service)
        if outcome.get("ok"):
            account = outcome.get("account") or ""
            return (f"'{plugin}' is now connected"
                    + (f" as {account}" if account else "")
                    + ". Its tools are available.")
        if outcome.get("stage") == "pending":
            return outcome.get("message", "Not approved yet.")
        return (f"[ERROR] '{plugin}' setup did not finish: "
                f"{outcome.get('message') or 'unknown error'}")

    if action in ("disconnect", "revoke", "logout"):
        if not state.get("requires_setup"):
            return f"'{plugin}' has no account to disconnect."
        google_oauth.revoke_token(state.get("service"))
        return (f"'{plugin}' disconnected — access revoked. "
                f"Run the setup again to reconnect it.")

    return (f"[ERROR] Unknown action '{action}'. Valid: list, status, enable, "
            f"disable, setup, finish, disconnect.")


# ── Sub-AI tool registry ──────────────────────────────────────────────────────
# Single source of truth for the tools a delegated sub-AI is allowed to run.
# It is built here (after every tool function is defined) so the entries can
# reference the functions directly.
#
# delegate_subtask() advertises only these tools to the sub-AI, which keeps
# the advertised schema list and the executable set in sync — a sub-AI can no
# longer be offered a tool whose dispatch would reject it.
#
# Deliberately NOT exposed to a sub-AI:
#   * delegate_subtask — recursion is not allowed for delegated workers
#   * initialize_project / add_subtask / update_subtask — these mutate the
#     parent agent's project state and belong to the main agent only
#   * retrieve_chunk / list_chunks — context browsing stays with the main agent
_SUB_TOOL_FUNCS: dict = {
    # Core file / shell tools
    "shell":                      shell,
    "read_file":                  read_file,
    "write_file":                 write_file,
    "find_replace":               find_replace,
    "index_files":                index_files,
    "search_in_files":            search_in_files,
    "search_files":               search_files,
    "list_directory":             list_directory,
    "rename_file":                rename_file,
    "delete_file":                delete_file,
    "http_request":               http_request,
    "get_datetime":               get_datetime,
    # Memory / delegation helpers
    "save_memory":                save_memory,
    "retrieve_memory":            retrieve_memory,
    "intermediate_print":         intermediate_print,
    "manage_todos":               manage_todos,
    "ask_user":                   ask_user,
    "confirm":                    confirm,
    "sleep_mode":                 sleep_mode,
    "run_diagnosis":              run_diagnosis,
    # Web / media
    "browse_web":                 browse_web,
    "browser":                    browser,
    "generate_image":             generate_image,
    # WhatsApp
    "send_whatsapp_message":      send_whatsapp_message,
    "get_whatsapp_status":        get_whatsapp_status,
    "get_whatsapp_chats":         get_whatsapp_chats,
    "get_pending_whatsapp_messages": get_pending_whatsapp_messages,
    "fetch_whatsapp_chat_history": fetch_whatsapp_chat_history,
    "set_whatsapp_busy_mode":     set_whatsapp_busy_mode,
    "get_whatsapp_report":        get_whatsapp_report,
    "set_whatsapp_user_profile":  set_whatsapp_user_profile,
    "archive_whatsapp_chat":      archive_whatsapp_chat,
    "search_whatsapp_chat":       search_whatsapp_chat,
    "get_whatsapp_group_participants": get_whatsapp_group_participants,
    "get_whatsapp_contact_info":  get_whatsapp_contact_info,
    "react_to_whatsapp_message":  react_to_whatsapp_message,
    "download_whatsapp_media":    download_whatsapp_media,
    "silence_whatsapp_contact":   silence_whatsapp_contact,
    "schedule_whatsapp_message":  schedule_whatsapp_message,
    "set_whatsapp_seen":          set_whatsapp_seen,
    # Google plugins (Gmail / Calendar / Drive)
    "send_gmail_message":         send_gmail_message,
    "get_gmail_messages":         get_gmail_messages,
    "search_gmail":               search_gmail,
    "read_gmail_message":         read_gmail_message,
    "get_gmail_profile":          get_gmail_profile,
    "get_gmail_labels":           get_gmail_labels,
    "create_gmail_label":         create_gmail_label,
    "delete_gmail_message":       delete_gmail_message,
    "list_calendars":             list_calendars,
    "get_calendar_events":        get_calendar_events,
    "create_calendar_event":      create_calendar_event,
    "update_calendar_event":      update_calendar_event,
    "delete_calendar_event":      delete_calendar_event,
    "list_drive_files":           list_drive_files,
    "upload_drive_file":          upload_drive_file,
    "download_drive_file":        download_drive_file,
    "delete_drive_file":          delete_drive_file,
    "create_drive_folder":        create_drive_folder,
    "share_drive_file":           share_drive_file,
    # Platform wrappers (tools/wrapper_*.py)
    "get_contacts":               get_contacts,
    "get_contact_by_id":          get_contact_by_id,
    "list_sensors":               list_sensors,
    "get_sensor_data":            get_sensor_data,
    "set_wallpaper":              set_wallpaper,
    "take_photo":                 take_photo,
    "record_video":               record_video,
    "record_audio":               record_audio,
    "pick_file":                  pick_file,
    "share_file":                 share_file,
    "share_text":                 share_text,
    "toggle_torch":               toggle_torch,
    "get_hostname":               get_hostname,
    "get_uptime":                 get_uptime,
}
