"""
Daily pipeline orchestrator.

Steps executed each run:
  1.  Backup the database
  2.  Download previous trading day's OHLCV (parallel, per stock)
  3.  Validate / sanitise each download
  4.  Detect retroactive corporate-action adjustments
  5.  Write good data to DB (one transaction per stock; failed = skip + log)
  6.  Record ingestion run in DB
  7.  Alert via WhatsApp if any symbols failed ingestion
  8.  Load all setups from Trading Setups/
  9.  Fetch stored OHLCV for each active stock
  10. Run every setup against every stock (parallel)
  11. Persist signals to DB
  12. Send WhatsApp alerts for signals that fired
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from config import MAX_WORKERS, NOTIFY_ON_INGESTION_FAILURE, NOTIFY_ON_SIGNAL
from data import db, collector, sanitizer
from data.stocks_list import NSE_500
from notifications.whatsapp import (
    send_ingestion_failure_alert, send_batch_signal_alert,
    send_analysis_started_alert,
)
from setup_loader import load_setups
from utils.backup import create_backup
from utils.logger import get_logger

log = get_logger(__name__)

# How many historical rows to load per stock for setup computation
_LOOKBACK = 200


# ── Step helpers ──────────────────────────────────────────────────────────────

def _ensure_stocks_registered() -> None:
    """Upsert every symbol in the NSE_500 universe into the stocks table."""
    for symbol, name, exchange in NSE_500:
        db.upsert_stock(symbol, exchange=exchange, name=name)


def _ingest_downloads(
    download_results: dict,
) -> tuple[list[str], list[str]]:
    """
    Validate and write downloads to DB.
    Returns (successful_symbols, failed_symbols).
    """
    successful: list[str] = []
    failed: list[str] = []

    for symbol, result in download_results.items():
        stock_id = db.get_stock_id(symbol)
        if stock_id is None:
            log.error(f"pipeline: {symbol} not in stocks table — skipping")
            failed.append(symbol)
            continue

        # Download failed entirely
        if result.df is None:
            log.warning(f"pipeline: {symbol} — download failed: {result.error}")
            failed.append(symbol)
            continue

        df = result.df

        # Validate
        val = sanitizer.validate(df, symbol)
        if not val.valid:
            log.error(f"pipeline: {symbol} validation FAILED: {val.errors}")
            failed.append(symbol)
            continue

        # Detect retroactive corporate-action adjustment
        last_date = db.get_last_date(stock_id)
        if last_date:
            # Get the close we have stored for that date
            hist = db.get_ohlcv(symbol, days=5)
            last_close: float | None = None
            if not hist.empty:
                matching = hist[hist.index.strftime("%Y-%m-%d") == last_date]
                if not matching.empty:
                    last_close = float(matching["close"].iloc[-1])

            if sanitizer.detect_adjustment(symbol, df, last_close, last_date):
                db.log_adjustment(
                    stock_id,
                    action_type="AUTO_DETECTED",
                    notes=f"Price deviation >50% detected on {last_date}. "
                          f"Stored close={last_close}, new adjusted close differs significantly.",
                )
                log.info(
                    f"pipeline: {symbol} — retroactive adjustment detected, "
                    f"re-downloading full history to realign"
                )
                # Re-download and replace all stored data
                from data.collector import download_history
                from config import HISTORY_DAYS
                full = download_history([symbol], days=HISTORY_DAYS)
                full_result = full.get(symbol)
                if full_result and full_result.df is not None:
                    val2 = sanitizer.validate(full_result.df, symbol)
                    if val2.valid:
                        db.replace_ohlcv(stock_id, full_result.df)
                        log.info(f"pipeline: {symbol} — full history replaced after adjustment")
                        successful.append(symbol)
                        continue
                log.error(f"pipeline: {symbol} — full history re-download also failed")
                failed.append(symbol)
                continue

        # Normal path: upsert new rows
        try:
            db.upsert_ohlcv(stock_id, df)
            successful.append(symbol)
        except Exception as exc:
            log.error(f"pipeline: {symbol} DB write failed — {exc}")
            failed.append(symbol)

    return successful, failed


def _run_setup_for_stock(setup, symbol: str) -> dict | None:
    """Run one setup against one stock's historical data. Returns signal dict or None."""
    try:
        df = db.get_ohlcv(symbol, days=_LOOKBACK)
        if df.empty:
            return None
        result = setup.signal(df, symbol)
        return result.to_dict()
    except Exception as exc:
        log.error(f"pipeline: setup '{setup.name}' on {symbol} failed — {exc}")
        return None


def _run_all_setups(
    setups,
    symbols: list[str],
    max_workers: int = MAX_WORKERS,
) -> list[dict]:
    """
    Run all setups × all stocks in parallel using ThreadPoolExecutor.
    Returns list of signal dicts for signals that fired (signal=True).
    """
    fired: list[dict] = []
    total_tasks = len(setups) * len(symbols)
    log.info(f"pipeline: running {len(setups)} setup(s) × {len(symbols)} stock(s) = {total_tasks} tasks")

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {
            pool.submit(_run_setup_for_stock, setup, symbol): (setup.name, symbol)
            for setup in setups
            for symbol in symbols
        }
        done = 0
        for future in as_completed(future_map):
            done += 1
            result = future.result()
            if result and result.get("signal"):
                fired.append(result)
            if done % 500 == 0 or done == total_tasks:
                log.info(f"pipeline: setups {done}/{total_tasks} done ({len(fired)} signals)")

    return fired


# ── Main entry point ──────────────────────────────────────────────────────────

def run_pipeline(symbols: list[str] | None = None) -> bool | None:
    """
    Execute the full daily pipeline.
    `symbols` defaults to all active NSE 100 stocks registered in the DB.
    """
    today = date.today().isoformat()
    log.info(f"{'='*60}")
    log.info(f"pipeline: START  run_date={today}")
    log.info(f"{'='*60}")

    # ── 0. Heads-up that the run has started (also warms up the WhatsApp bridge)
    if NOTIFY_ON_SIGNAL:
        send_analysis_started_alert("Stock Analysis", today)

    # ── 1. Backup ─────────────────────────────────────────────────────────────
    backup_path = create_backup()
    if backup_path:
        log.info(f"pipeline: backup → {backup_path.name}")
    else:
        log.warning("pipeline: backup skipped or failed — proceeding anyway")

    # ── 2. Ensure all stocks are registered ──────────────────────────────────
    _ensure_stocks_registered()

    # Determine which symbols to process
    if symbols is None:
        active = db.get_active_stocks()
        symbols = [s["symbol"] for s in active]

    if not symbols:
        log.warning("pipeline: no symbols to process — exiting")
        return

    # ── 3. Download ───────────────────────────────────────────────────────────
    download_results = collector.download_yesterday(symbols)

    # ── 4–6. Ingest ───────────────────────────────────────────────────────────
    successful, failed = _ingest_downloads(download_results)
    status = "SUCCESS" if not failed else ("PARTIAL_FAILURE" if successful else "FAILED")
    db.log_ingestion_run(
        status=status,
        total=len(symbols),
        successful=len(successful),
        failed=len(failed),
        failed_symbols=failed,
    )
    log.info(f"pipeline: ingestion {status} — {len(successful)} ok, {len(failed)} failed")

    # ── 7. Alert on failures ──────────────────────────────────────────────────
    if failed and NOTIFY_ON_INGESTION_FAILURE:
        send_ingestion_failure_alert(failed, today)

    if status == "FAILED":
        log.error("pipeline: all downloads failed — aborting setup run")
        return

    # ── 7b. Fill realised returns for past picks now that new data has arrived ─
    try:
        from analytics.outcomes import update_outcomes
        n_closed = update_outcomes()
        if n_closed:
            log.info(f"pipeline: outcome tracker — {n_closed} pick(s) reached their horizon")
    except Exception as exc:
        log.warning(f"pipeline: outcome update skipped — {exc}")

    # ── 8. Load setups ────────────────────────────────────────────────────────
    setups = load_setups()
    if not setups:
        log.warning("pipeline: no setups found — nothing to analyse")
        return

    # ── 9–11. Run setups and persist signals ─────────────────────────────────
    fired_signals = _run_all_setups(setups, symbols)

    for sig in fired_signals:
        stock_id = db.get_stock_id(sig["symbol"])
        if stock_id:
            db.upsert_signal(
                stock_id=stock_id,
                setup_name=sig["setup_name"],
                signal_date=sig["date"],
                signal=sig["signal"],
                metadata=sig.get("metadata"),
            )

    log.info(f"pipeline: {len(fired_signals)} signal(s) fired")

    # ── 12. WhatsApp alert (picks, or a 'no setups today' note if none clear) ─
    # Called even with zero fired signals so a quiet day still sends a heads-up.
    notify_ok = True
    if NOTIFY_ON_SIGNAL:
        notify_ok = send_batch_signal_alert(fired_signals, today)

    # ── 13. Weekly pick-performance scorecard (sent on SCORECARD_WEEKDAY) ─────
    try:
        from analytics.outcomes import send_weekly_scorecard_if_due
        send_weekly_scorecard_if_due()
    except Exception as exc:
        log.warning(f"pipeline: weekly scorecard skipped — {exc}")

    log.info(f"pipeline: DONE  run_date={today}")
    return notify_ok


# ── CLI helper ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the Signal Infomer pipeline manually")
    parser.add_argument(
        "--symbols",
        nargs="*",
        help="Specific symbols to process (defaults to all active NSE 100)",
    )
    args = parser.parse_args()

    db.init_db()
    ok = run_pipeline(symbols=args.symbols or None)
    if ok is False:
        sys.exit(1)
