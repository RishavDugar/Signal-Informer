"""
News analyzer pipeline — orchestrates the full flow:

  1. Check Ollama is available
  2. Fetch news from all sources (parallel)
  3. Deduplicate against recently recommended symbols
  4. Analyze with Ollama (Pass 1 + Pass 2)
  5. Enrich with recent price data from the OHLCV DB
  6. Save recommendations to DB
  7. Format and send WhatsApp message(s)
  8. Mark sent; purge expired records

Called every morning at NEWS_SCHEDULE_HOUR (default 7 AM IST) by scheduler.py.
Can also be run directly:  python -m news_analyzer.pipeline
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

from data.db import get_ohlcv, init_db
from news_analyzer.analyzer import analyze_news
from news_analyzer.db import (
    get_recently_recommended,
    get_recently_scouted,
    get_today_recommendations,
    mark_scout_whatsapp_sent,
    mark_whatsapp_sent,
    purge_expired,
    save_recommendations,
    save_scout_recommendations,
)
from news_analyzer.fetcher import fetch_all_news
from news_analyzer.formatter import format_messages, format_scout_messages
from news_analyzer.ollama_client import (
    available_model, ensure_available, server_status, warmup_model, unload_model,
)
from news_analyzer.web_scout import ALL_SCOUTS, run_scout
from utils.logger import get_logger

log = get_logger("news_pipeline")


# ── Technical indicator helpers ───────────────────────────────────────────────
#
# Standalone calculations rather than importing from "Trading Setups/_indicators.py"
# — that directory name contains a space and isn't a valid package path; it's only
# importable there via setup_loader.py's runtime sys.path manipulation.

def _rsi(closes, period: int = 14) -> float | None:
    """Wilder-smoothed RSI of the last value in a close-price Series."""
    if len(closes) < period + 1:
        return None
    delta    = closes.diff()
    gains    = delta.clip(lower=0)
    losses   = (-delta).clip(lower=0)
    avg_gain = gains.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = losses.ewm(com=period - 1, min_periods=period).mean()
    last_gain, last_loss = avg_gain.iloc[-1], avg_loss.iloc[-1]
    if last_loss == 0:
        return 100.0 if last_gain > 0 else 50.0
    rs  = last_gain / last_loss
    rsi = 100 - (100 / (1 + rs))
    return round(float(rsi), 1)


# ── On-demand OHLCV fetch (NOT persisted) ─────────────────────────────────────
#
# Scout lenses (small-cap growth especially, see SMALL_CAP_GROWTH) can surface
# symbols outside the NSE_100 universe tracked in db/market_data.db. For those,
# pull just enough history straight from yfinance to compute technicals for the
# WhatsApp message — mirrors data/collector.py._fetch_ticker's download/clean
# pattern, but the result is used in-memory only and never written to the DB.

def _fetch_recent_ohlcv(symbol: str, days: int = 60) -> pd.DataFrame:
    """
    Fetch the last `days` trading rows for symbol directly from yfinance.
    Raises on any problem — caller is expected to catch and degrade gracefully.
    """
    end   = date.today() + timedelta(days=1)
    start = end - timedelta(days=days * 2 + 10)  # headroom for weekends/holidays

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
        raise ValueError(f"empty response for {symbol}")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] for col in df.columns]
    df.columns = [c.lower() for c in df.columns]

    missing = {"open", "high", "low", "close", "volume"} - set(df.columns)
    if missing:
        raise ValueError(f"missing columns {missing} for {symbol}")

    df = df[["open", "high", "low", "close", "volume"]].copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.sort_index(inplace=True)
    df = df.dropna(subset=["open", "high", "low", "close"])
    if df.empty:
        raise ValueError(f"no complete rows for {symbol}")

    return df.tail(days)


# ── Price + technical enrichment ──────────────────────────────────────────────

def _enrich_technicals(rec: dict) -> dict:
    """
    Add price + technical context:
      - CMP, 1D/5D/20D % change
      - RSI(14)            — momentum / overbought-oversold
      - Volume ratio       — today's volume vs 20-day average (surge detection)
      - 20-SMA trend       — price vs 20-day simple moving average

    Tries the local OHLCV DB first (covers the tracked NSE_100 universe);
    falls back to an on-demand yfinance fetch for off-list symbols (scout
    picks aren't restricted to NSE_100). That fetched data is used only for
    this calculation and is never persisted.

    Fails gracefully — fields stay absent (and the WhatsApp message still
    sends without technical lines) if both lookups come up short.
    """
    symbol = rec["symbol"]
    try:
        df = get_ohlcv(symbol, days=60)
        if df.empty or len(df) < 2:
            df = _fetch_recent_ohlcv(symbol, days=60)
        if df.empty or len(df) < 2:
            return rec

        closes = df["close"]
        last_close  = float(closes.iloc[-1])
        prev_close  = float(closes.iloc[-2])
        change_1d   = (last_close - prev_close) / prev_close * 100 if prev_close else None

        def _pct_change(n: int) -> float | None:
            if len(df) < n + 1:
                return None
            past = float(closes.iloc[-(n + 1)])
            return (last_close - past) / past * 100 if past else None

        rec["cmp"]            = round(last_close, 2)
        rec["change_1d_pct"]  = round(change_1d, 2) if change_1d is not None else None
        rec["change_5d_pct"]  = round(_pct_change(5), 2)  if _pct_change(5)  is not None else None
        rec["change_20d_pct"] = round(_pct_change(20), 2) if _pct_change(20) is not None else None

        # RSI(14)
        rec["rsi14"] = _rsi(closes, 14)

        # Volume ratio vs trailing 20-day average (excluding today)
        if len(df) >= 21:
            avg_vol   = float(df["volume"].iloc[-21:-1].mean())
            today_vol = float(df["volume"].iloc[-1])
            rec["volume_ratio"] = round(today_vol / avg_vol, 1) if avg_vol else None

        # Trend vs 20-day SMA
        if len(df) >= 20:
            sma20 = float(closes.iloc[-20:].mean())
            if sma20:
                rec["sma20_trend"] = "above" if last_close > sma20 else "below"
                rec["sma20_pct"]   = round((last_close - sma20) / sma20 * 100, 1)
    except Exception as exc:
        log.debug(f"news_pipeline: technical enrich failed for {symbol} — {exc}")
    return rec


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_news_pipeline() -> None:
    today = date.today().isoformat()
    log.info("=" * 60)
    log.info(f"news_pipeline: START  date={today}")
    log.info("=" * 60)

    # ── 0. Heads-up that the run has started (also warms up the WhatsApp bridge)
    # Broadcast to the news recipient list (WHATSAPP_PHONES) so the heads-up
    # reaches the same audience as the picks that follow — not just the owner.
    from config import NOTIFY_ON_SIGNAL, WHATSAPP_PHONES
    from notifications.whatsapp import send_analysis_started_alert
    if NOTIFY_ON_SIGNAL:
        send_analysis_started_alert("News Analysis", today, WHATSAPP_PHONES)

    # ── 1. Verify Ollama (auto-starts if not running) ────────────────────────
    if not ensure_available():
        log.error("news_pipeline: Ollama unavailable — aborting. "
                  "Install from https://ollama.com then run: ollama pull qwen3:8b")
        return
    log.info(f"news_pipeline: using model '{available_model()}'")
    server_status()

    # ── 1b. Warm up: force model into VRAM before the large Pass 1 prompt ───
    if not warmup_model():
        log.error("news_pipeline: model warmup failed — model may not be downloaded. "
                  f"Run: ollama pull {available_model()}")
        return

    # ── 2. Fetch news ─────────────────────────────────────────────────────────
    articles = fetch_all_news()
    if not articles:
        log.warning("news_pipeline: no articles fetched — aborting")
        return

    # ── 3. Dedup: filter out recently recommended symbols ────────────────────
    recent_syms = get_recently_recommended()

    # ── 4. Analyze with Ollama (main news picks) ─────────────────────────────
    from concurrent.futures import ThreadPoolExecutor
    from config import NEWS_TOP_N, SCOUT_DEDUP_DAYS
    from notifications.whatsapp import send_news_picks_alert

    log.info(f"news_pipeline: analyzing {len(articles)} articles with Ollama "
             f"(model={available_model()}) — long step, GPU-bound, ~1-3 min...")
    picks = analyze_news(articles, top_n=NEWS_TOP_N)
    log.info(f"news_pipeline: Ollama analysis complete — {len(picks)} candidate pick(s)")

    if not picks:
        log.warning("news_pipeline: Ollama returned no picks — aborting")
        unload_model()
        return

    # Filter out already-recommended stocks
    new_picks = [p for p in picks if p["symbol"] not in recent_syms]
    skipped   = len(picks) - len(new_picks)
    if skipped:
        log.info(f"news_pipeline: {skipped} pick(s) skipped (already recommended)")

    run_date_str = date.today().strftime("%d %b %Y")
    subtitles = {
        "hidden_gems"     : "Hidden gems  |  contrarian + under-the-radar  |  Ollama AI",
        "small_cap_growth": "Emerging small-caps  |  growth potential  |  Ollama AI",
        "smart_money"     : "Broking-house ratings + super-investor moves  |  Ollama AI",
    }

    # WhatsApp sends run on a background single-worker thread, so each search
    # type's message is dispatched the moment that search finishes — overlapping
    # with the remaining (Ollama) searches — instead of batching every send to
    # the very end. One worker keeps sends ordered and avoids hitting the
    # WhatsApp bridge with concurrent requests; the 30s SQLite busy-timeout (WAL)
    # makes the background mark-sent writes safe alongside the main thread saves.
    sender    = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wa-send")
    send_futs = []

    def _send_main(recs: list[dict]) -> None:
        try:
            if send_news_picks_alert(format_messages(recs, run_date=run_date_str)):
                mark_whatsapp_sent([r["id"] for r in recs])
                log.info(f"news_pipeline: main WhatsApp sent — {len(recs)} picks")
            else:
                log.error("news_pipeline: main WhatsApp send failed")
        except Exception as exc:
            log.exception(f"news_pipeline: main WhatsApp send errored — {exc}")

    def _send_scout(key, label, tag, subtitle, picks_, inserted_ids) -> None:
        try:
            msgs = format_scout_messages(picks_, label=label, tag=tag,
                                         subtitle=subtitle, run_date=run_date_str)
            if send_news_picks_alert(msgs):
                mark_scout_whatsapp_sent(inserted_ids)
                log.info(f"news_pipeline: '{key}' WhatsApp sent — {len(picks_)} picks")
            else:
                log.error(f"news_pipeline: '{key}' WhatsApp send failed")
        except Exception as exc:
            log.exception(f"news_pipeline: '{key}' WhatsApp send errored — {exc}")

    # ── 4a. Main picks: enrich, save, then fire the send right away ──────────
    if new_picks:
        for p in new_picks:
            _enrich_technicals(p)
            p["rec_date"]     = today
            p["company_name"] = p.get("company", "")

        if not save_recommendations(new_picks):
            log.warning("news_pipeline: no new recommendations saved (all duplicates?)")

        recs_to_send = get_today_recommendations()
        if recs_to_send:
            send_futs.append(sender.submit(_send_main, recs_to_send))
    else:
        log.info("news_pipeline: all main picks already recommended recently")

    # ── 4b–5. Each scout lens: search, then immediately dispatch its message ──
    # Each lens's recently-surfaced symbols are looked up FIRST and fed to the
    # model as negative context (exclude_symbols), so the search hunts for fresh
    # names. The send for one lens then overlaps the NEXT lens's Ollama search —
    # investors receive each lens as soon as it's ready, not all at the end.
    scout_counts: dict[str, int] = {}
    for config in ALL_SCOUTS:
        recent = get_recently_scouted(config.key)
        log.info(f"news_pipeline: starting scout pass '{config.key}' ({config.label})... "
                 f"({len(recent)} recently-scouted symbol(s) excluded)")
        found = run_scout(config, top_n=3, exclude_symbols=recent)
        scout_counts[config.key] = len(found or [])

        if not found:
            log.info(f"news_pipeline: no picks generated for scout '{config.key}'")
            continue

        # Dedup backstop: don't resurface a symbol this lens scouted recently.
        new_scout_picks = [p for p in found if p["symbol"] not in recent]
        skipped = len(found) - len(new_scout_picks)
        if skipped:
            log.info(f"news_pipeline: '{config.key}' — {skipped} pick(s) skipped "
                     f"(scouted within last {SCOUT_DEDUP_DAYS}d)")
        if not new_scout_picks:
            log.info(f"news_pipeline: '{config.key}' — nothing new to send")
            continue

        for p in new_scout_picks:
            _enrich_technicals(p)
            p["rec_date"] = today

        inserted_ids = save_scout_recommendations(config.key, new_scout_picks)
        send_futs.append(sender.submit(
            _send_scout, config.key, config.label, config.tag,
            subtitles.get(config.key, ""), new_scout_picks, inserted_ids))

    # ── 6. All inference done — free VRAM while sends finish in the background ─
    unload_model()

    # ── 7. Wait for every queued send to finish, then purge expired records ──
    for f in send_futs:
        f.result()
    sender.shutdown(wait=True)
    purge_expired()

    scout_summary = "  ".join(f"{k}={v}" for k, v in scout_counts.items())
    log.info(f"news_pipeline: DONE  main_picks={len(new_picks)}  {scout_summary}")


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    run_news_pipeline()
