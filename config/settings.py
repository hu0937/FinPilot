import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    load_dotenv(_env_file)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH  = DATA_DIR / "quant.db"
LOG_DIR  = DATA_DIR / "logs"
PID_DIR  = BASE_DIR / "scripts" / "pids"

VENV_PYTHON = "python3"  # 或指定虛擬環境路徑

# --- API Keys ---
FINMIND_TOKEN     = os.getenv("FINMIND_TOKEN", "")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# --- Telegram Allowlist ---
# 逗號分隔的 Telegram user ID，例如：TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
# 未設定時，bot 拒絕所有人（fail-safe）
_raw_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: set[int] = {int(x.strip()) for x in _raw_ids.split(",") if x.strip().isdigit()}

HISTORY_DAYS   = 60  # days of price history to fetch

# --- Claude Model ---
CLAUDE_MODEL     = os.getenv("CLAUDE_MODEL", "claude-opus-4-6")       # 開發環境 / agent
BOT_CLAUDE_MODEL = os.getenv("BOT_CLAUDE_MODEL", "claude-sonnet-4-6") # Telegram Bot

