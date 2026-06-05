#!/data/data/com.termux/files/usr/bin/bash
set -e

echo "[setup] Installing system packages..."
pkg install -y python rust git cmake clang which

export ANDROID_API_LEVEL=24

echo "[setup] Installing Python packages..."
pip install openai requests beautifulsoup4 jsonschema

echo "[setup] Creating directory structure..."
mkdir -p config data logs workspace docs/patches

echo "[setup] Creating config/api.keys from template if missing..."
if [ ! -f config/api.keys ]; then
  cat > config/api.keys << 'KEYS'
{
  "google": ["YOUR_GOOGLE_API_KEY_HERE"],
  "nvidia": [],
  "groq":   []
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
