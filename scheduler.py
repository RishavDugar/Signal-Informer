"""
APScheduler-based daily trigger.

Fires the full pipeline at SCHEDULE_HOUR:SCHEDULE_MINUTE in Asia/Kolkata timezone.
Runs Monday–Friday (Indian market days); weekends are skipped automatically
because yfinance returns no data for non-trading days — the pipeline handles
empty downloads gracefully.

Usage:
    python scheduler.py
    (runs continuously; Ctrl-C to stop)
"""

import signal
import sys
import time

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from config import (
    SCHEDULE_HOUR, SCHEDULE_MINUTE, SCHEDULE_TZ,
    NEWS_SCHEDULE_HOUR, NEWS_SCHEDULE_MINUTE,
)
from data.db import init_db
from news_analyzer.pipeline import run_news_pipeline
from notifications.whatsapp import ensure_bridge_ready
from pipeline import run_pipeline
from utils.logger import get_logger

log = get_logger(__name__)


def _news_job() -> None:
    """7 AM — news analyzer: fetch, analyze, send AI-powered picks."""
    try:
        ensure_bridge_ready()   # bring up the headless WhatsApp sender if needed
        run_news_pipeline()
    except Exception as exc:
        log.exception(f"scheduler: unhandled exception in news pipeline — {exc}")


def _job() -> None:
    """8 AM — technical pipeline: ingest OHLCV, run setups, send signals."""
    try:
        ensure_bridge_ready()   # bring up the headless WhatsApp sender if needed
        run_pipeline()
    except Exception as exc:
        log.exception(f"scheduler: unhandled exception in pipeline — {exc}")


def start() -> None:
    # Ensure the DB schema exists before anything else
    init_db()

    # Pre-warm + self-check the headless WhatsApp bridge at launch, so a missing
    # or expired session is surfaced now (loud WARNING in the log) rather than
    # silently at the first 7/8 AM send. The bridge runs headless/detached — no
    # terminal stays open; only the one-time QR scan is interactive.
    ensure_bridge_ready()

    tz = pytz.timezone(SCHEDULE_TZ)
    scheduler = BlockingScheduler(timezone=tz)

    news_trigger = CronTrigger(
        hour=NEWS_SCHEDULE_HOUR,
        minute=NEWS_SCHEDULE_MINUTE,
        day_of_week="mon-fri",
        timezone=tz,
    )
    scheduler.add_job(
        _news_job, news_trigger,
        id="news_pipeline", name="7 AM News + AI stock picks",
    )

    trigger = CronTrigger(
        hour=SCHEDULE_HOUR,
        minute=SCHEDULE_MINUTE,
        day_of_week="mon-fri",
        timezone=tz,
    )
    scheduler.add_job(_job, trigger=trigger, id="daily_pipeline", name="Daily market data + setups")

    log.info(
        f"scheduler: news picks  at {NEWS_SCHEDULE_HOUR:02d}:{NEWS_SCHEDULE_MINUTE:02d} {SCHEDULE_TZ} (Mon-Fri)"
    )
    log.info(
        f"scheduler: tech signals at {SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d} "
        f"{SCHEDULE_TZ} (Mon-Fri)"
    )
    log.info("scheduler: running — press Ctrl-C to stop")

    def _shutdown(signum, frame):
        log.info("scheduler: shutdown signal received")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler: stopped")


if __name__ == "__main__":
    start()
