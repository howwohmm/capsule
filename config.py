import os
from pathlib import Path
from dotenv import load_dotenv

# Always load .env from the project directory, regardless of CWD
_here = Path(__file__).parent
load_dotenv(_here / ".env", override=True)

OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
GROQ_API_KEY        = os.getenv("GROQ_API_KEY", "")
RESEND_API_KEY      = os.getenv("RESEND_API_KEY", "")
GMAIL_USER          = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD  = os.getenv("GMAIL_APP_PASSWORD", "")
FROM_EMAIL          = os.getenv("FROM_EMAIL", "MindOS <learn@mindos.so>")
GOOGLE_SHEET_ID    = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_CREDS_JSON  = os.getenv("GOOGLE_CREDS_JSON", "")   # raw JSON string in env
DATABASE_PATH      = os.getenv("DATABASE_PATH", "mindos.db")
CRON_SECRET        = os.getenv("CRON_SECRET", "")
ADMIN_SECRET       = os.getenv("ADMIN_SECRET", "")
ADMIN_USER         = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD     = os.getenv("ADMIN_PASSWORD", "")
BASE_URL           = os.getenv("BASE_URL", "http://localhost:8000")

LLM_MODEL        = os.getenv("LLM_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
LLM_BUDGET_CAP   = float(os.getenv("LLM_BUDGET_CAP", "1.0"))   # USD — hard stop
PASTE_RS_URL = "https://paste.rs"
DPASTE_URL   = "https://dpaste.org/api/"

# How many videos to process in parallel (keep low to respect rate limits)
WORKER_CONCURRENCY = 2
