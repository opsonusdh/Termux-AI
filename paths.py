import os

ROOT = os.path.expanduser("~/ai_root")
ORCHESTRATION_DIR = os.path.join(ROOT, "orchestration")
TOOLS_DIR = os.path.join(ROOT, "tools")
REFLECTION_DIR = os.path.join(ROOT, "reflection")
DATA_DIR = os.path.join(ROOT, "data")
DOCS_DIR = os.path.join(ROOT, "docs")
LOGS_DIR = os.path.join(ROOT, "logs")
WORKSPACE_DIR = os.path.join(ROOT, "workspace")
INSTRUCTIONS_DIR = os.path.join(ROOT, "instructions")
REFLECTION_LOG_FILE = os.path.join(LOGS_DIR, "reflection.jsonl")
