"""
Signal Infomer — Initialisation Script

Run this once to set up the system, or again to wipe and start fresh:

    python initialize.py

What it does on every run:
  0. (Re-run only) Backs up the existing database, then wipes all data
     so the new run starts from a completely clean state.
  1. Creates all DB tables and verifies integrity
  2. Registers all NSE 100 stocks
  3. Downloads HISTORY_DAYS of historical OHLCV
  4. Validates and ingests the data
  5. Creates an initial database backup
  6. Runs the backtester to compute per-setup win-rates (days 1-5)
     and writes db/strategy_weights.json

Setup signals are NOT run here — they are computed on every daily
pipeline run. This keeps initialisation fast.

Progress is printed to stdout and logged to logs/signal_infomer.log.
"""

from __future__ import annotations

import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from config import DB_PATH, BACKUP_DIR, HISTORY_DAYS
from data import db, collector, sanitizer
from data.stocks_list import NSE_500
from utils.backup import create_backup
from utils.logger import get_logger

log = get_logger(__name__)

_WEIGHTS_PATH = DB_PATH.parent / "strategy_weights.json"


# ── Step 0: wipe existing data ────────────────────────────────────────────────

def _wipe_previous_data() -> None:
    """
    If a previous database exists:
      1. Take a safety backup labelled 'pre_reinit'
      2. Delete the database file (and any WAL / SHM leftovers)
      3. Delete strategy_weights.json so stale weights don't carry over
    """
    if not DB_PATH.exists():
        return   # first run — nothing to wipe

    print("\n[0/6] Previous database detected — wiping for a clean restart...")

    # Safety backup before destruction
    stamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
    pre_bak = BACKUP_DIR / f"market_data_pre_reinit_{stamp}.db"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(DB_PATH, pre_bak)
        print(f"  [OK] Safety backup saved: {pre_bak.name}")
    except Exception as exc:
        log.warning(f"init: could not save safety backup — {exc}")
        print(f"  [!]  Safety backup failed ({exc}) — continuing anyway")

    # Delete the database + WAL/SHM journal files
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            try:
                p.unlink()
                log.info(f"init: deleted {p.name}")
            except Exception as exc:
                log.error(f"init: could not delete {p} — {exc}")
                print(f"  [!]  Could not delete {p.name}: {exc}")
                sys.exit(1)

    # Delete stale weights so the backtester writes a fresh file
    if _WEIGHTS_PATH.exists():
        _WEIGHTS_PATH.unlink()
        log.info("init: deleted strategy_weights.json")

    print("  [OK] All previous data wiped. Starting fresh.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n" + "=" * 64)
    print("  Signal Infomer - Initialisation")
    print("=" * 64)

    # ── 0. Wipe previous data (if any) ───────────────────────────────────────
    _wipe_previous_data()

    # ── 1. Init DB ────────────────────────────────────────────────────────────
    print("\n[1/6] Initialising database schema...")
    db.init_db()
    if not db.integrity_check():
        print("  [FAIL] DB integrity check failed - aborting.")
        sys.exit(1)
    print("  [OK] Database OK")

    # ── 2. Register stocks ────────────────────────────────────────────────────
    print(f"\n[2/6] Registering {len(NSE_500)} NSE 500 stocks...")
    for symbol, name, exchange in NSE_500:
        db.upsert_stock(symbol, exchange=exchange, name=name)
    print(f"  [OK] {len(NSE_500)} stocks registered")

    # ── 3. Download history ───────────────────────────────────────────────────
    symbols = [sym for sym, _, _ in NSE_500]
    today   = date.today()
    start   = today - timedelta(days=HISTORY_DAYS)

    print(f"\n[3/6] Downloading {HISTORY_DAYS}-day history for {len(symbols)} stocks...")
    print(f"      Period: {start} -> {today}")
    print("      (may take 5-15 minutes on first run with HISTORY_DAYS=1000)")

    results = collector.download_many(symbols, start=start, end=today)

    # ── 4. Ingest ─────────────────────────────────────────────────────────────
    print("\n[4/6] Validating and ingesting data...")
    ok_count       = 0
    failed_symbols: list[str] = []

    for symbol, result in results.items():
        stock_id = db.get_stock_id(symbol)
        if stock_id is None:
            continue

        if result.df is None:
            log.warning(f"init: {symbol} - download failed: {result.error}")
            failed_symbols.append(symbol)
            continue

        val = sanitizer.validate(result.df, symbol)
        if not val.valid:
            log.error(f"init: {symbol} - validation failed: {val.errors}")
            failed_symbols.append(symbol)
            continue

        try:
            db.upsert_ohlcv(stock_id, result.df)
            ok_count += 1
        except Exception as exc:
            log.error(f"init: {symbol} DB write failed - {exc}")
            failed_symbols.append(symbol)

    print(f"  [OK] {ok_count} stocks ingested")
    if failed_symbols:
        preview = ", ".join(failed_symbols[:10])
        extra   = " ..." if len(failed_symbols) > 10 else ""
        print(f"  [!]  {len(failed_symbols)} failed: {preview}{extra}")
        print("       Check logs/signal_infomer.log for details.")

    db.log_ingestion_run(
        status="SUCCESS" if not failed_symbols else "PARTIAL_FAILURE",
        total=len(symbols),
        successful=ok_count,
        failed=len(failed_symbols),
        failed_symbols=failed_symbols,
        notes=f"Historical bootstrap: {HISTORY_DAYS} days",
    )

    # ── 5. Create initial backup ──────────────────────────────────────────────
    print("\n[5/6] Creating initial database backup...")
    bp = create_backup()
    if bp:
        print(f"  [OK] Backup: {bp.name}")
    else:
        print("  [!]  Backup skipped (DB may be empty)")

    # ── 6. Run backtester ────────────────────────────────────────────────────
    print("\n[6/6] Running backtester to compute strategy win-rates (days 1-5)...")
    print("      (may take 5-10 minutes)")
    try:
        from backtester import run_backtest, save_weights, WEIGHTS_PATH, MAX_HOLD_DAYS
        bt_results = run_backtest()
        save_weights(bt_results)

        # Print day-by-day table
        header = f"  {'Setup':<30} " + " ".join(f"d{d:>4}" for d in range(1, MAX_HOLD_DAYS + 1)) + f"  {'Best':>8}  {'Weight':>7}"
        print(f"\n{header}")
        print("  " + "-" * (len(header) - 2))
        for name, s in sorted(bt_results.items(), key=lambda x: -x[1]["weight"]):
            by_day = s.get("by_day", {})
            day_wrs = " ".join(
                f"{by_day.get(str(d), {}).get('win_rate', 0):>5.1%}"
                for d in range(1, MAX_HOLD_DAYS + 1)
            )
            best = f"d{s['best_days']} {s['best_win_rate']:.1%}"
            print(f"  {name:<30} {day_wrs}  {best:>8}  {s['weight']:>6.3f}")

        print(f"\n  [OK] Weights saved to {WEIGHTS_PATH}")
    except Exception as exc:
        log.error(f"init: backtester failed - {exc}")
        print(f"  [!]  Backtester failed: {exc}")
        print("       Run  python backtester.py  separately to generate weights.")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 64)
    print("  Initialisation complete")
    print(f"  Stocks ingested : {ok_count}/{len(symbols)}")
    print("=" * 64)

    print("\n" + "-" * 64)
    print("Next steps:")
    print("  1. Open web.whatsapp.com in Chrome and log in")
    print("  2. Register the Windows Task Scheduler job (runs daily at 8am):")
    print("       python setup_windows_task.py")
    print("  3. Trigger a manual run to see today's signals:")
    print("       python pipeline.py")
    print("  4. Re-run the backtester weekly as more data accumulates:")
    print("       python backtester.py")
    print("-" * 64 + "\n")


if __name__ == "__main__":
    main()
