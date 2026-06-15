"""
Market data collector — downloads OHLCV from yfinance.

Key design decisions:
  - auto_adjust=True: yfinance adjusts all historical prices for splits/bonuses
  - ThreadPoolExecutor: network I/O is the bottleneck; threads are ideal
  - tenacity retries: transient network failures are retried with exp back-off
  - Each stock download is fully isolated — one failure does not affect others
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import NamedTuple

import pandas as pd
import yfinance as yf
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import MAX_WORKERS, RETRY_ATTEMPTS, RETRY_DELAY_SECONDS
from utils.logger import get_logger

log = get_logger(__name__)

# Columns we expect from yfinance (after normalising to lowercase)
_REQUIRED_COLS = {"open", "high", "low", "close", "volume"}


class DownloadResult(NamedTuple):
    symbol: str
    df: pd.DataFrame | None    # None means failure
    error: str | None


# ── Retry-decorated single-ticker fetch ──────────────────────────────────────

@retry(
    stop=stop_after_attempt(RETRY_ATTEMPTS),
    wait=wait_exponential(multiplier=RETRY_DELAY_SECONDS, min=RETRY_DELAY_SECONDS, max=60),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _fetch_ticker(symbol: str, start: date, end: date) -> pd.DataFrame:
    """
    Download OHLCV for one symbol between [start, end).
    Returns a DataFrame with lowercase column names and DatetimeIndex.
    Raises on empty result or missing columns — triggers retry.

    Uses yf.download() (batch endpoint) which is more reliable than
    Ticker.history() under yfinance 1.x cookie/auth changes.
    threads=False prevents yf.download from spawning its own thread pool
    inside our ThreadPoolExecutor.
    """
    df = yf.download(
        symbol,
        start=start.isoformat(),
        end=end.isoformat(),
        auto_adjust=True,
        progress=False,
        threads=False,
        actions=False,
    )

    if df is None or df.empty:
        raise ValueError(f"Empty response for {symbol}")

    # yf.download with a single symbol returns a regular DataFrame but may have
    # a MultiIndex columns ("Price", "Ticker") — flatten to single level
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] for col in df.columns]

    # Normalise column names to lowercase
    df.columns = [c.lower() for c in df.columns]

    # yfinance occasionally returns duplicate-named columns (e.g. both "Close"
    # and "Adj Close" collapsing to "close" under some auto_adjust paths) —
    # keep the first occurrence so downstream df[col] always yields a Series.
    if df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated()]

    missing = _REQUIRED_COLS - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns {missing} for {symbol}")

    df = df[list(_REQUIRED_COLS)].copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)  # strip timezone
    df.sort_index(inplace=True)

    # Drop rows with NaN in price columns — yfinance sometimes includes the
    # current (incomplete) trading day as a partial row with NaN OHLC values.
    df = df.dropna(subset=["open", "high", "low", "close"])

    if df.empty:
        raise ValueError(f"No complete rows after dropping NaN prices for {symbol}")

    return df


# ── Public API ────────────────────────────────────────────────────────────────

def download_single(symbol: str, start: date, end: date) -> DownloadResult:
    """Download one stock; returns DownloadResult (df=None on failure)."""
    try:
        df = _fetch_ticker(symbol, start, end)
        log.debug(f"collector: {symbol} → {len(df)} rows")
        return DownloadResult(symbol=symbol, df=df, error=None)
    except Exception as exc:
        log.warning(f"collector: {symbol} FAILED — {exc}")
        return DownloadResult(symbol=symbol, df=None, error=str(exc))


def download_many(
    symbols: list[str],
    start: date,
    end: date,
    max_workers: int = MAX_WORKERS,
) -> dict[str, DownloadResult]:
    """
    Parallel download for a list of symbols.
    Returns dict mapping symbol → DownloadResult.
    Failed symbols have df=None.

    After the main pass, any symbols that returned None are retried once
    sequentially (with a 5 s pause) to recover from transient rate-limits.
    """
    results: dict[str, DownloadResult] = {}
    total = len(symbols)
    log.info(f"collector: downloading {total} symbols [{start} -> {end}]")

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_sym: dict = {}
        # Stagger submissions in batches of 10 to avoid hitting yfinance rate-limits
        for i, sym in enumerate(symbols):
            future_to_sym[pool.submit(download_single, sym, start, end)] = sym
            if (i + 1) % 10 == 0:
                time.sleep(0.5)

        done = 0
        for future in as_completed(future_to_sym):
            result = future.result()
            results[result.symbol] = result
            done += 1
            if done % 20 == 0 or done == total:
                ok = sum(1 for r in results.values() if r.df is not None)
                log.info(f"collector: {done}/{total} done ({ok} ok)")

    # ── Retry pass for any that failed ───────────────────────────────────────
    failed_syms = [sym for sym, r in results.items() if r.df is None]
    if failed_syms:
        log.info(
            f"collector: {len(failed_syms)} failed — retrying after 10 s pause: "
            f"{', '.join(failed_syms)}"
        )
        time.sleep(10)
        for sym in failed_syms:
            retry_result = download_single(sym, start, end)
            results[sym] = retry_result
            if retry_result.df is not None:
                log.info(f"collector: retry OK — {sym}")
            else:
                log.warning(f"collector: retry FAILED — {sym}: {retry_result.error}")

    ok_count = sum(1 for r in results.values() if r.df is not None)
    fail_count = total - ok_count
    log.info(f"collector: finished — {ok_count} ok, {fail_count} failed")
    return results


def download_yesterday(symbols: list[str]) -> dict[str, DownloadResult]:
    """
    Download the previous trading day's data for the given symbols.
    Uses a 7-day lookback window to account for weekends and holidays
    (yfinance returns only actual trading days within the range).
    """
    today = date.today()
    start = today - timedelta(days=7)
    return download_many(symbols, start=start, end=today)


def download_history(symbols: list[str], days: int = 100) -> dict[str, DownloadResult]:
    """Download the last `days` calendar days of history for each symbol."""
    today = date.today()
    start = today - timedelta(days=days)
    return download_many(symbols, start=start, end=today)
