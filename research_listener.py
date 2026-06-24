"""
On-demand stock-research listener.

Watches the research WhatsApp group (RESEARCH_GROUP, default = the NewsAnalysis
group) for a "<trigger> <SYMBOL>" message — e.g.

    Search RELIANCE.NS

— and replies in the same group with price + technicals and a fresh web-news
insight for that stock (see news_analyzer.research.research_symbol).

This is a long-running process: the bridge keeps inbound messages in an in-memory
ring buffer, so something must poll it continuously. The poll loop mirrors the
WorldQuant engine's WhatsApp-trigger watcher.

Usage:
    python research_listener.py        # run in the foreground (Ctrl-C to stop)

The headless WhatsApp bridge is auto-started from its saved session if needed, so
no terminal needs to stay open for the bridge itself — only this watcher process.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

# When launched windowless via pythonw.exe (the Windows startup task — no console,
# so sys.stdout/stderr are None), redirect output to a log file BEFORE importing
# anything that creates a logger, so log handlers + uncaught tracebacks are still
# captured. Run interactively with python.exe and output stays in the terminal.
if sys.stdout is None or sys.executable.lower().endswith("pythonw.exe"):
    _log_path = Path(__file__).parent / "logs" / "research_listener.log"
    _log_path.parent.mkdir(exist_ok=True)
    sys.stdout = sys.stderr = open(_log_path, "a", encoding="utf-8", buffering=1)

from config import (
    RESEARCH_GROUP, RESEARCH_TRIGGER, WHATSAPP_BACKEND, WHATSAPP_POLL_INTERVAL_S,
)
from notifications.whatsapp import (
    bridge_health, ensure_bridge_ready, poll_group, send_to_group,
)
from utils.logger import get_logger

log = get_logger("research_listener")

# Request format (single or multi-line):
#   Search BLIL
#   Keywords: Balmer Lawrie, Investments, PSU
# Line 1: "<trigger> <SYMBOL>" (SYMBOL drives price/technicals). Optional later
# "Keywords:" line: comma-separated news search terms (the first is the company
# name) — lets a ticker whose news never uses it (BLIL → 'Balmer Lawrie') get news.
_REQUEST_RE  = re.compile(rf"^\s*{re.escape(RESEARCH_TRIGGER)}\s+([A-Za-z0-9.&\-]+)",
                          re.IGNORECASE)
_KEYWORDS_RE = re.compile(r"(?im)^\s*keywords?\s*[:\-]\s*(.+)$")


def _parse_keywords(body: str) -> list[str]:
    m = _KEYWORDS_RE.search(body)
    if not m:
        return []
    return [k.strip() for k in m.group(1).split(",") if k.strip()]


def _handle(symbol: str, keywords: list[str], sender: str) -> None:
    """Run research for one request and post the result back to the group."""
    log.info(f"listener: request for {symbol!r} keywords={keywords} from {sender!r}")
    send_to_group(RESEARCH_GROUP, f"🔎 Researching *{symbol.upper()}* — one moment...")
    # Imported lazily so a quick parse failure doesn't pay the heavy import cost.
    from news_analyzer.research import research_symbol
    try:
        message = research_symbol(symbol, keywords=keywords)
    except Exception as exc:
        log.exception(f"listener: research crashed for {symbol!r} — {exc}")
        message = (f"*Stock Research — {symbol.upper()}*\n"
                   f"❌ Research failed unexpectedly. Please try again shortly.")
    send_to_group(RESEARCH_GROUP, message)


def _watch_loop() -> None:
    since = int(time.time() * 1000)   # only react to messages from now on
    log.info(f"listener: watching group {RESEARCH_GROUP!r} for "
             f"'{RESEARCH_TRIGGER} <SYMBOL>' (poll every {WHATSAPP_POLL_INTERVAL_S}s)")
    while True:
        try:
            # Self-heal the bridge only when it's actually down — a quiet /status
            # probe each cycle (no logging), escalating to a full autostart just
            # when needed, so the log isn't spammed with a "READY" line every poll.
            if not bridge_health().get("ready"):
                ensure_bridge_ready()
            for m in poll_group(RESEARCH_GROUP, since):
                since = max(since, int(m.get("ts", since)))
                body  = (m.get("body") or "").strip()
                match = _REQUEST_RE.match(body)
                if match:
                    _handle(match.group(1), _parse_keywords(body), m.get("from", "?"))
        except Exception as exc:
            log.warning(f"listener: watch loop error — {exc}")
        time.sleep(WHATSAPP_POLL_INTERVAL_S)


def main() -> int:
    if WHATSAPP_BACKEND != "bridge":
        log.error("listener: requires WHATSAPP_BACKEND=bridge (group polling is "
                  "bridge-only) — exiting")
        return 1
    if not RESEARCH_GROUP:
        log.error("listener: no RESEARCH_GROUP / WHATSAPP_NEWS_GROUP configured — exiting")
        return 1
    # Best-effort warm-up only — do NOT exit if the bridge is briefly unready (e.g.
    # mid-reconnect at logon). Exiting here left the listener dead until a manual
    # restart; instead we enter the watch loop and let poll/send re-establish the
    # bridge on demand (it self-heals), so a transient blip never kills the watcher.
    if not ensure_bridge_ready():
        log.warning("listener: WhatsApp bridge not ready at startup — entering the watch "
                    "loop anyway; it will be retried on demand as requests arrive")
    try:
        _watch_loop()
    except KeyboardInterrupt:
        log.info("listener: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
