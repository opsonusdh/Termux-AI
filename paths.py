"""
paths.py — Single source of truth for all absolute paths in Termux-AI and Windows-AI.

Every module imports from here. Never hardcode absolute paths.
"""
import os

# Project root is the directory that contains this file.
ROOT = os.path.dirname(os.path.abspath(__file__))

# ── Sub-package directories ────────────────────────────────────────────────
CORE_DIR          = os.path.join(ROOT, "core")
AGENT_DIR         = os.path.join(ROOT, "agent")
ORCHESTRATION_DIR = os.path.join(ROOT, "orchestration")
REFLECTION_DIR    = os.path.join(ROOT, "reflection")
TOOLS_DIR         = os.path.join(ROOT, "tools")
INSTRUCTIONS_DIR  = os.path.join(ROOT, "instructions")
WORKSPACE_DIR     = os.path.join(ROOT, "workspace")

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
# Google OAuth client credentials for the Gmail / Calendar / Drive plugins.
# Kept out of api.keys on purpose: api.keys holds *provider API keys* and is
# gated in every permission mode, while this file holds an OAuth *client* id
# and secret that the user generates once in Google Cloud Console.
GOOGLE_OAUTH_FILE     = os.path.join(CONFIG_DIR, "google_oauth.json")
# Per-service OAuth tokens (access + refresh). Directory mode 0700, files 0600.
GOOGLE_TOKENS_DIR     = os.path.join(DATA_DIR,   "google_tokens")
# Pending authorization state (PKCE verifier + redirect URI) so a consent
# code can be pasted in later, from another device, if the browser handoff
# did not happen on this machine.
GOOGLE_PENDING_FILE   = os.path.join(DATA_DIR,   "google_pending_auth.json")
CAPABILITY_REGISTRY   = os.path.join(CONFIG_DIR, "capability_registry.json")
MEMORY_FILE           = os.path.join(ROOT,       "memories.txt")
INDEXED_MEMORY_FILE   = os.path.join(ROOT,       "indexed_memory.txt")
PROMPT_FILE           = os.path.join(CORE_DIR,   "PROMPT.md")
REFLECTION_LOG_FILE   = os.path.join(LOGS_DIR,   "reflection.jsonl")
# Legacy machine log (turn/tool bookkeeping).  Nothing reads it any more —
# the live session log is LOG_FILE below — but the constant stays so an
# external script that points at it keeps working.
HISTORY_FILE          = os.path.join(LOGS_DIR,   "history.jsonl")
# The live session log.  Everything the runtime does — user input, reasoning,
# tool calls, tool results, delegation traffic, replies — is appended here
# and flushed immediately, so `tail -f logs/log.txt` shows the work in
# progress even if the process is killed mid-turn.  Capped at 1,000,000
# lines; the oldest lines are dropped when it grows past that.
LOG_FILE              = os.path.join(LOGS_DIR,   "log.txt")
CHUNKS_FILE           = os.path.join(LOGS_DIR,   "chunks.jsonl")
CHUNK_SUMMARIES_FILE  = os.path.join(LOGS_DIR,   "chunk_summaries.json")
# Where the active conversation context starts.  "/session new" moves this
# marker forward: the chunks before it stay on disk (and stay retrievable
# with retrieve_chunk) but are no longer injected into the model's context.
# It is persisted so a restart does not bring the old conversation back.
CONTEXT_SESSION_FILE  = os.path.join(DATA_DIR,   "context_session.json")
# Compressed context segments.  When the assembled history no longer fits the
# model's context window, the oldest passes (reasoning details, tool calls and
# tool outputs) are replaced by a digest and the raw text is written here.
# Append-only; the model pulls the raw text back with retrieve_archive(id).
CONTEXT_ARCHIVE_FILE  = os.path.join(LOGS_DIR,   "context_archive.jsonl")
