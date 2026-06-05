"""
paths.py — Single source of truth for all absolute paths in Termux-AI.

Every module imports from here. Never hardcode ~/Termux-AI elsewhere.
"""
import os

# Project root is the directory that contains this file.
ROOT = os.path.dirname(os.path.abspath(__file__))

# ── Sub-package directories ────────────────────────────────────────────────
CORE_DIR         = os.path.join(ROOT, "core")
AGENT_DIR        = os.path.join(ROOT, "agent")
ORCHESTRATION_DIR = os.path.join(ROOT, "orchestration")
REFLECTION_DIR   = os.path.join(ROOT, "reflection")
TOOLS_DIR        = os.path.join(ROOT, "tools")
INSTRUCTIONS_DIR = os.path.join(ROOT, "instructions")
WORKSPACE_DIR    = os.path.join(ROOT, "workspace")

# ── Data / config / logs ──────────────────────────────────────────────────
DATA_DIR   = os.path.join(ROOT, "data")
CONFIG_DIR = os.path.join(ROOT, "config")
LOGS_DIR   = os.path.join(ROOT, "logs")
DOCS_DIR   = os.path.join(ROOT, "docs")

# ── Specific files ────────────────────────────────────────────────────────
STATE_FILE            = os.path.join(DATA_DIR,   "state.json")
VALIDATOR_SCHEMA_FILE = os.path.join(DATA_DIR,   "validator_schema.json")
CONFIG_FILE           = os.path.join(CONFIG_DIR, "config.json")
API_KEYS_FILE         = os.path.join(CONFIG_DIR, "api.keys")
CAPABILITY_REGISTRY   = os.path.join(CONFIG_DIR, "capability_registry.json")
MEMORY_FILE           = os.path.join(ROOT,       "memories.txt")
INDEXED_MEMORY_FILE   = os.path.join(ROOT,       "indexed_memory.txt")
PROMPT_FILE           = os.path.join(CORE_DIR,       "PROMPT.md")
REFLECTION_LOG_FILE   = os.path.join(LOGS_DIR,   "reflection.jsonl")
HISTORY_FILE          = os.path.join(LOGS_DIR,   "history.jsonl")
CHUNKS_FILE           = os.path.join(LOGS_DIR,   "chunks.jsonl")
CHUNK_SUMMARIES_FILE  = os.path.join(LOGS_DIR,   "chunk_summaries.json")

