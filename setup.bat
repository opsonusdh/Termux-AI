@echo off
echo [setup] Installing Python packages...
python -m pip install -U pip setuptools wheel
pip install openai requests beautifulsoup4 jsonschema

echo [setup] Creating directory structure...
if not exist config mkdir config
if not exist data mkdir data
if not exist logs mkdir logs
if not exist workspace mkdir workspace
if not exist docs\patches mkdir docs\patches

echo [setup] Creating config\api.keys from template if missing...
if not exist config\api.keys (
  (
    echo {
    echo   "google": ["YOUR_GOOGLE_API_KEY_HERE"],
    echo   "nvidia": [],
    echo   "groq":   [],
    echo   "openrouter": []
    echo }
  ) > config\api.keys
  echo   -^> config\api.keys created. Fill in your API keys.
)

echo [setup] Creating config\config.json from defaults if missing...
if not exist config\config.json (
  (
    echo {
    echo   "stt_path": "Termux-STT",
    echo   "tts_enabled": false,
    echo   "use_groq": false
    echo }
  ) > config\config.json
)

echo [setup] Done. Run with: python core
pause
