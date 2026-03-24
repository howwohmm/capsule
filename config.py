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
ALERT_EMAIL        = os.getenv("ALERT_EMAIL", "")   # where to send error alerts (set to your email)
BASE_URL           = os.getenv("BASE_URL", "http://localhost:8000")

LLM_MODEL        = os.getenv("LLM_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
# Fallback models tried in order when primary is rate-limited (comma-separated)
LLM_MODEL_FALLBACKS = [
    m.strip() for m in
    os.getenv(
        "LLM_MODEL_FALLBACKS",
        "google/gemini-2.0-flash-001:free,deepseek/deepseek-chat:free"
    ).split(",")
    if m.strip()
]
LLM_BUDGET_CAP   = float(os.getenv("LLM_BUDGET_CAP", "1.0"))   # USD — hard stop
PASTE_RS_URL = "https://paste.rs"
DPASTE_URL   = "https://dpaste.org/api/"

# Optional HTTP/SOCKS proxy for YouTube requests (bypasses cloud IP blocking)
# e.g. "http://user:pass@host:port" or "socks5://host:port"
YOUTUBE_PROXY    = os.getenv("YOUTUBE_PROXY", "")

# Webshare rotating residential proxy credentials (preferred over YOUTUBE_PROXY)
# Get from: https://dashboard.webshare.io/proxy/settings → "Proxy Username" / "Proxy Password"
# This uses WebshareProxyConfig which rotates IPs automatically — better than static datacenter proxies
WEBSHARE_PROXY_USER = os.getenv("WEBSHARE_PROXY_USER", "")
WEBSHARE_PROXY_PASS = os.getenv("WEBSHARE_PROXY_PASS", "")

# Path to a Netscape cookies.txt file exported from a YouTube-logged-in browser
# Bypasses YouTube bot detection without needing a proxy
# Export with: chrome extension "Get cookies.txt LOCALLY" or "EditThisCookie"
# Upload to server: scp cookies.txt root@157.245.103.89:/app/youtube_cookies.txt
YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "/app/youtube_cookies.txt")

SENTRY_DSN = os.getenv("SENTRY_DSN", "")

# CORS allowed origins (comma-separated in env)
CORS_ORIGINS = [
    o.strip() for o in
    os.getenv("CORS_ORIGINS", "https://capsule.ohm.quest").split(",")
    if o.strip()
]

# How many videos to process in parallel (keep low to respect rate limits)
WORKER_CONCURRENCY = 2
