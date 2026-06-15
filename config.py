from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

BASE_DIR = Path(__file__).parent

# Database
DB_PATH = BASE_DIR / os.getenv("DB_PATH", "db/market_data.db")
BACKUP_DIR = BASE_DIR / os.getenv("BACKUP_DIR", "db/backups")
MAX_BACKUPS = int(os.getenv("MAX_BACKUPS", "7"))

# WhatsApp
# Send backend:
#   "bridge"    — headless Node service (whatsapp-web.js / Puppeteer). Sends over
#                 the WhatsApp Web protocol, NOT GUI keystrokes, so it works with
#                 the screen off / device locked. Recommended. One-time QR scan.
#   "pywhatkit" — legacy GUI automation. Only works while the desktop is unlocked
#                 and focused; fails silently when the screen is off / locked.
WHATSAPP_BACKEND = os.getenv("WHATSAPP_BACKEND", "bridge").lower()
# Local URL of the Node bridge (binds to localhost only).
WHATSAPP_BRIDGE_URL = os.getenv("WHATSAPP_BRIDGE_URL", "http://127.0.0.1:8765")
# Optional shared secret; if set here it must match BRIDGE_TOKEN in the Node env.
WHATSAPP_BRIDGE_TOKEN = os.getenv("WHATSAPP_BRIDGE_TOKEN", "")
# If the bridge isn't reachable when a send is attempted, try to launch it
# (headless, detached) and wait for it to become ready. Requires a prior QR scan.
WHATSAPP_BRIDGE_AUTOSTART = os.getenv("WHATSAPP_BRIDGE_AUTOSTART", "true").lower() == "true"
# Where the Node bridge lives and how long to wait for it to become ready (s).
# Defaults to the bundled bridge inside the shared `whatsapp_bridge` package
# (github.com/RishavDugar/WhatsApp-Bridge); override with an env path if needed.
from whatsapp_bridge import bundled_bridge_dir as _bundled_bridge_dir
_wa_bridge_dir_env = os.getenv("WHATSAPP_BRIDGE_DIR")
WHATSAPP_BRIDGE_DIR = (BASE_DIR / _wa_bridge_dir_env) if _wa_bridge_dir_env else _bundled_bridge_dir()
WHATSAPP_BRIDGE_READY_TIMEOUT = int(os.getenv("WHATSAPP_BRIDGE_READY_TIMEOUT", "75"))

WHATSAPP_PHONE = os.getenv("WHATSAPP_PHONE", "")
# Recipients for the news-analyzer stock-pick alerts (main picks + all scout
# lenses): comma-separated phone numbers, each WITH country code, e.g.
#   WHATSAPP_PHONES=+919876543210,+919876500000
# Falls back to the single WHATSAPP_PHONE if WHATSAPP_PHONES isn't set, so
# existing single-recipient setups keep working unchanged.
_news_phones_raw = os.getenv("WHATSAPP_PHONES", "")
WHATSAPP_PHONES = [p.strip() for p in _news_phones_raw.split(",") if p.strip()] \
    or ([WHATSAPP_PHONE] if WHATSAPP_PHONE else [])
NOTIFY_ON_SIGNAL = os.getenv("NOTIFY_ON_SIGNAL", "true").lower() == "true"
NOTIFY_ON_INGESTION_FAILURE = os.getenv("NOTIFY_ON_INGESTION_FAILURE", "true").lower() == "true"
# Minimum conviction-weighted avg return (fraction) for a stock to appear in alerts.
# 0.005 = 0.5% average return per trade; setups with negative expected value are excluded.
# NOTE: this threshold is applied to the NET (after-cost) average return — see
# TRANSACTION_COST below — so it screens on what an investor actually keeps.
MIN_AVG_RETURN = float(os.getenv("MIN_AVG_RETURN", "0.005"))

# Minimum conviction-weighted win rate (0.0-1.0) for a stock to appear in alerts.
# A stock must clear BOTH this and MIN_AVG_RETURN. 0.0 = no confidence gate
# (avg-return screen only). Example: 0.55 = only alert when the blended historical
# win rate is >= 55%.
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.0"))

# Round-trip transaction cost (fraction of notional) charged against every
# backtested trade before its return is booked. Covers brokerage + STT +
# exchange/SEBI fees + GST + a slippage allowance. Gross backtest returns
# systematically overstate the realisable edge; netting costs at the trade
# level is what makes win-rate, expectancy and conviction weights honest.
#   Indian equities, all-in round trip:
#     delivery (multi-day longs): ~0.20% fees + ~0.10-0.20% slippage
#     intraday  (squared-off shorts): lower fees, but slippage-dominated
# 0.0030 (30 bps) is a single conservative blended default; tune per broker.
TRANSACTION_COST = float(os.getenv("TRANSACTION_COST", "0.0030"))

# Confidence level for the Wilson lower-bound win rate reported to investors.
# 0.90 => we report the win rate we are 90% confident the setup beats. This is
# deliberately pessimistic for small samples (it widens the penalty as n shrinks)
# and conservative against the best-day selection bias in the backtester.
WR_CONFIDENCE = float(os.getenv("WR_CONFIDENCE", "0.90"))

# Data collection
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "20"))
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "3"))
RETRY_DELAY_SECONDS = int(os.getenv("RETRY_DELAY_SECONDS", "5"))
HISTORY_DAYS = int(os.getenv("HISTORY_DAYS", "100"))
# How many days of stored OHLCV the backtester loads per stock.
# Should be >= HISTORY_DAYS; the extra headroom lets rolling windows
# warm up before the first signal bar is measured.
BACKTEST_WINDOW_DAYS = int(os.getenv("BACKTEST_WINDOW_DAYS", "1100"))
# Number of exit days tested by the backtester (1 through MAX_HOLD_DAYS).
# d=1 = D1 close (same-day hold); d=10 = D10 close (ten-day hold).
MAX_HOLD_DAYS = int(os.getenv("MAX_HOLD_DAYS", "10"))
# Tie-break threshold for avg return (e.g. 0.005 = 0.5 percentage points).
# If an earlier day's avg return is within this margin of the peak, prefer
# the earlier day (shorter hold is better when outcomes are nearly equal).
BEST_DAY_THRESHOLD = float(os.getenv("BEST_DAY_THRESHOLD", "0.005"))

# Scheduler — technical pipeline (8 AM IST)
SCHEDULE_HOUR   = int(os.getenv("SCHEDULE_HOUR",   "8"))
SCHEDULE_MINUTE = int(os.getenv("SCHEDULE_MINUTE", "0"))
SCHEDULE_TZ     = os.getenv("SCHEDULE_TZ",         "Asia/Kolkata")

# Scheduler — news pipeline (7 AM IST, runs before the technical pipeline)
NEWS_SCHEDULE_HOUR   = int(os.getenv("NEWS_SCHEDULE_HOUR",   "7"))
NEWS_SCHEDULE_MINUTE = int(os.getenv("NEWS_SCHEDULE_MINUTE", "0"))

# Ollama (local inference)
OLLAMA_HOST  = os.getenv("OLLAMA_HOST",  "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "")  # empty = auto-detect best qwen model

# News analyzer
NEWS_DEDUP_DAYS = int(os.getenv("NEWS_DEDUP_DAYS", "28"))   # days before re-recommending a symbol
NEWS_TOP_N      = int(os.getenv("NEWS_TOP_N",      "5"))    # max picks per morning run

# Scout passes (hidden gems / small-cap growth / smart money)
SCOUT_DEDUP_DAYS = int(os.getenv("SCOUT_DEDUP_DAYS", "5"))  # days before re-surfacing a scout pick

# ── Outcome tracking / performance scorecard ──────────────────────────────────
# Trailing window (days) summarised by the pick-performance scorecard.
SCORECARD_DAYS = int(os.getenv("SCORECARD_DAYS", "30"))
# Weekday the weekly WhatsApp scorecard is sent (0=Mon .. 6=Sun). Default 4=Fri.
SCORECARD_WEEKDAY = int(os.getenv("SCORECARD_WEEKDAY", "4"))

# Ensure directories exist at import time
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
(BASE_DIR / "logs").mkdir(parents=True, exist_ok=True)
