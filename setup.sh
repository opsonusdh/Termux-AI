#!/usr/bin/env bash
set -e

# The project root — the script's own directory, so it works from anywhere.
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

if command -v pkg >/dev/null 2>&1; then
  echo "[setup] Termux detected. Updating system packages..."
  pkg update -y && pkg upgrade -y || true
  # chromium is what the browser tools drive (browse_web / browser).  The rest
  # are the build tools the optional STT component needs.
  pkg install -y python rust git cmake clang which make pkg-config openssl libffi chromium || true
fi

echo "[setup] Installing/upgrading Python packages..."
python -m pip install -U pip setuptools wheel
# pygments powers the syntax highlighting of code blocks, tool calls and the
# permission prompt.  The renderer falls back to plain text without it.
pip install openai httpx requests beautifulsoup4 jsonschema pygments
# Optional: a richer prompt (multi-line editing, persistent history).  The
# runtime falls back to plain input() when it is not installed.
pip install prompt_toolkit || true

echo "[setup] Creating directory structure..."
mkdir -p "$ROOT_DIR/config" "$ROOT_DIR/data" "$ROOT_DIR/logs" "$ROOT_DIR/workspace" "$ROOT_DIR/docs/patches"

echo "[setup] Creating config/api.keys from template if missing..."
if [ ! -f "$ROOT_DIR/config/api.keys" ]; then
  cat > "$ROOT_DIR/config/api.keys" << 'KEYS'
{
  "google":     ["YOUR_GOOGLE_API_KEY_HERE"],
  "nvidia":     [],
  "groq":       [],
  "openrouter": [],
  "ollama":     [],
  "ollama-local": ["No-Need"]
}
KEYS
  echo "  → config/api.keys created. Fill in your API keys."
fi

echo "[setup] Creating config/config.json from defaults if missing..."
if [ ! -f "$ROOT_DIR/config/config.json" ]; then
  # Unquoted heredoc: the STT path has to be absolute, and only the shell can
  # know where this checkout lives.  The keys match core/app_config.py.
  cat > "$ROOT_DIR/config/config.json" << CFG
{
  "stt_path": "$ROOT_DIR/Termux-STT",
  "tts_enabled": false,
  "use_groq": false,
  "show_details": false,
  "autonomous": false,
  "notify": true,
  "spill_trigger_tokens": 4000,
  "spill_enabled": true
}
CFG
fi

echo "[setup] Done. Run with: python core"
