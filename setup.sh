#!/usr/bin/env bash
set -e

if command -v pkg >/dev/null 2>&1; then
  echo "[setup] Termux detected. Updating system packages..."
  pkg update -y && pkg upgrade -y || true
  pkg install -y python rust git cmake clang which make pkg-config openssl libffi || true
fi

echo "[setup] Installing/upgrading Python packages..."
python -m pip install -U pip setuptools wheel
pip install openai requests beautifulsoup4 jsonschema

echo "[setup] Creating directory structure..."
mkdir -p config data logs workspace docs/patches

echo "[setup] Creating config/api.keys from template if missing..."
if [ ! -f config/api.keys ]; then
  cat > config/api.keys << 'KEYS'
{
  "google": ["YOUR_GOOGLE_API_KEY_HERE"],
  "nvidia": [],
  "groq":   [],
  "openrouter": []
}
KEYS
  echo "  → config/api.keys created. Fill in your API keys."
fi

echo "[setup] Creating config/config.json from defaults if missing..."
if [ ! -f config/config.json ]; then
  cat > config/config.json << 'CFG'
{
  "stt_path": "Termux-STT",
  "tts_enabled": false,
  "use_groq": false
}
CFG
fi

echo "[setup] Done. Run with: python core"
