"""
Signal Infomer — Strategy Backtester

Rolls a window over stored OHLCV data for every (setup, stock) pair.

For every fired signal at bar t, the outcome is tested at two exit prices
per holding day:
  open  → open[t+d]   (gap-open fill — early exit)
  close → close[t+d]  (end-of-day exit — hold the full session)

Each day's win rate is max(open_wr, close_wr).  Both are stored and surfaced
in WhatsApp messages so you can see whether the edge comes from early or late
exits.

The optimal holding period (best win-rate across both exits) is selected per
setup and used:
  • As the weight for the conviction-ranking score
  • Surfaced in the WhatsApp message: "Best: d2 WR 68% (n=45) | d1: 62%"

Win-rate is Bayesian-smoothed (10 pseudo-obs at 50%) to prevent small-sample
extremes. The weight formula maps win-rate → [0.10, 2.00].

Parallelism
-----------
ProcessPoolExecutor is used — each worker is a separate OS process with its
own Python interpreter, so there is NO shared GIL.  This gives true CPU
parallelism for the bar-level Python loop that ThreadPoolExecutor cannot
parallelise (the GIL is held during Python iteration even when numpy releases
it during EWM/rolling calls).

Architecture:
  • Stock data is serialised with pickle once in the main process and sent to
    every worker at startup via the pool initialiser — transferred once per
    worker, not once per task.
  • Each task carries only small picklable values (setup file path, class name,
    params dict, symbol, stride).  The worker re-loads and caches the setup
    class on first use, then reuses it for subsequent tasks.
  • All (setup × stock) pairs are submitted to ONE flat pool so all CPUs stay
    busy even when setups have very different runtimes.

_backtest_setup is kept sequential (max_workers=1) for the hyperparameter
search which already parallelises at the combination level via its own pool.

Results written to db/strategy_weights.json.

Usage:
    python backtester.py                # full run, all CPUs
    python backtester.py --quick        # stride=3, ~40% faster
    python backtester.py --workers 8    # cap worker processes
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import pickle
import sys
import time

import numpy as np
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import (
    DB_PATH, BACKTEST_WINDOW_DAYS, MAX_HOLD_DAYS, BEST_DAY_THRESHOLD,
    MIN_AVG_RETURN, TRANSACTION_COST, WR_CONFIDENCE,
)
from data.db import get_active_stocks, get_ohlcv
from notifications.whatsapp import _direction_of
from setup_loader import load_setups
from utils.logger import get_logger

log = get_logger("backtester")

WEIGHTS_PATH      = DB_PATH.parent / "strategy_weights.json"
_CPU_COUNT        = os.cpu_count() or 4

PRIOR_N           = 10      # Bayesian pseudo-observations
PRIOR_WR          = 0.50    # Prior win rate — neutral (50 %)
# Minimum observations for a holding day to compete as "best day".
# Was 5 — picking a holding period from 5 trades is curve-fitting, not
# evidence. 20 keeps the day-selection honest while still letting
# moderately rare setups qualify.
MIN_OBS_FOR_BEST  = int(os.getenv("MIN_OBS_FOR_BEST", "20"))
# MAX_HOLD_DAYS and BEST_DAY_THRESHOLD imported from config

# z-scores for common one-sided confidence levels — used by the Wilson
# lower-bound win rate. Falls back to 1.2816 (90 %) for unlisted levels.
_Z_BY_CONF = {0.80: 0.8416, 0.85: 1.0364, 0.90: 1.2816, 0.95: 1.6449, 0.975: 1.9600}
_WR_Z      = _Z_BY_CONF.get(round(WR_CONFIDENCE, 3), 1.2816)


def _wilson_lower_bound(wins: int, n: int, z: float = _WR_Z) -> float:
    """
    Wilson score interval lower bound for a binomial proportion (win rate).

    Why this instead of the raw win rate: with n trades observed, the true win
    rate could plausibly be lower than what we saw — and the fewer trades, the
    further it could be off. The Wilson lower bound is the win rate we can be
    WR_CONFIDENCE-confident the setup actually exceeds. It punishes small
    samples automatically and is conservative against the optimistic bias from
    selecting the best holding day on the same data. n=0 -> 0.0.
    """
    if n <= 0:
        return 0.0
    p  = wins / n
    z2 = z * z
    denom  = 1.0 + z2 / n
    centre = p + z2 / (2 * n)
    margin = z * ((p * (1 - p) + z2 / (4 * n)) / n) ** 0.5
    return max(0.0, (centre - margin) / denom)


def _mean_lower_bound(ret_sum: float, ret_sq_sum: float, n: int,
                      z: float = _WR_Z) -> tuple[float, float, float]:
    """
    One-sided lower confidence bound on the MEAN net return, plus the std-dev
    and t-statistic: returns (ret_lower, ret_std, t_stat).

    Why this matters economically: an investor trades the EXPECTANCY, and the
    win rate alone says nothing about whether the average profit is real or
    noise. avg = +1% over 30 trades with a 12% per-trade std has a t-stat of
    ~0.45 — indistinguishable from luck — while the same +1% with a 2% std
    (t ≈ 2.7) is tradeable evidence. ret_lower = avg − z·(std/√n) is the
    expectancy we are WR_CONFIDENCE-confident the setup actually beats; it
    automatically punishes small samples and high dispersion, which also
    shrinks the optimism from selecting the best holding day in-sample.
    n < 2 → no variance estimate → (0.0, 0.0, 0.0).
    """
    if n < 2:
        return 0.0, 0.0, 0.0
    mean = ret_sum / n
    var  = max(0.0, (ret_sq_sum - n * mean * mean) / (n - 1))
    std  = var ** 0.5
    if std == 0.0:
        return mean, 0.0, 0.0
    se = std / n ** 0.5
    return mean - z * se, std, mean / se


# ── Per-day accumulator ───────────────────────────────────────────────────────
# One dict per holding-day per exit bucket (open / close). Beyond the raw return
# sum and win count we also track win_sum / loss_sum / loss_n so the summariser
# can report avg-win, avg-loss and profit factor — the magnitude information a
# win rate alone hides.

_DAY_KEYS = (
    "open_ret_sum",  "open_ret_sq",  "open_n",  "open_wins",  "open_win_sum",  "open_loss_sum",  "open_loss_n",
    "close_ret_sum", "close_ret_sq", "close_n", "close_wins", "close_win_sum", "close_loss_sum", "close_loss_n",
)


def _empty_day() -> dict:
    return {k: (0.0 if k.endswith(("sum", "sq")) else 0) for k in _DAY_KEYS}


def _book(bucket: dict, prefix: str, ret: float) -> None:
    """Record one trade's net return into the open_/close_ fields of `bucket`."""
    bucket[f"{prefix}_ret_sum"] += ret
    bucket[f"{prefix}_ret_sq"]  += ret * ret
    bucket[f"{prefix}_n"]       += 1
    if ret > 0:
        bucket[f"{prefix}_wins"]     += 1
        bucket[f"{prefix}_win_sum"]  += ret
    elif ret < 0:
        bucket[f"{prefix}_loss_n"]   += 1
        bucket[f"{prefix}_loss_sum"] += ret


# ── Weight formula ────────────────────────────────────────────────────────────

def _compute_weight(avg_return: float, n: int, ret_lower: float | None = None) -> float:
    """
    Conviction weight in [0.10, 2.00], anchored at 1.00 = neutral.

    Economic rationale: conviction should scale with the EVIDENCE of positive
    net expectancy, not the point estimate. The point-estimate avg return is
    biased upward (best holding day + best exit are selected on the same data)
    and says nothing about dispersion. So the weight is driven by ret_lower —
    the one-sided lower confidence bound on the mean net return — which already
    embeds the small-sample penalty (z·std/√n), so no extra Bayesian shrink is
    applied on top (that would double-penalise).

        weight = clip(1.0 + ret_lower × 20, 0.10, 2.00)

    n=0                 → 1.00  (no data → neutral)
    ret_lower = 0%      → 1.00  (cannot rule out zero edge → neutral)
    ret_lower = +5%     → 2.00  (strong, statistically solid edge)
    ret_lower negative  → < 1.00 even when avg is positive (weak evidence)
    avg < MIN_AVG_RETURN→ 0.10  (fails the investor's minimum-return screen)

    ret_lower=None preserves the legacy behaviour (Bayesian-smoothed avg) for
    old stats dicts that lack variance data.
    """
    if n == 0:
        return 1.0
    if avg_return < MIN_AVG_RETURN:
        return 0.10   # below the minimum return threshold — negligible conviction
    if ret_lower is None:
        ret_lower = (avg_return * n) / (n + PRIOR_N)
    base = 1.0 + ret_lower * 20.0
    return round(max(0.10, min(2.00, base)), 3)


# ── Per-stock worker (called from thread pool) ────────────────────────────────

def _process_one_stock(
    setup,
    symbol: str,
    df,
    stride: int,
    cost: float = 0.0,
) -> tuple[dict, dict, int, int, int, int]:
    """
    Slide a window over one stock for one setup.
    Returns (partial_long, partial_short, n_long, n_short, n_long_sl, n_short_sl).

    Long / neutral signals → partial_long (d=1 close .. d=10 open/close)
    Short / sell signals   → partial_short (d=1 close ONLY — intraday, squared off)

    Performance is tracked separately so backtested metrics for each direction
    are never contaminated by the other.

    `cost` is the round-trip transaction cost (fraction of notional) subtracted
    from every trade's return before it is booked, so win/loss classification
    and all downstream stats are net of costs. Defaults to 0.0 so unit tests of
    the raw entry/exit mechanics are unaffected; production passes
    config.TRANSACTION_COST.
    """
    partial_long  = {d: _empty_day() for d in range(1, MAX_HOLD_DAYS + 1)}
    partial_short = {d: _empty_day() for d in range(1, MAX_HOLD_DAYS + 1)}
    n_long = n_short = n_long_sl = n_short_sl = 0
    n = len(df)

    # ── Collect signal events: (bar_idx, direction, native_sl_price) ─────────
    # Fast path: setups exposing vector_signals() compute the whole signal
    # series in one vectorised pass (stride is ignored — every bar is tested,
    # which both speeds things up ~100x and increases sample size).
    # Slow path: legacy per-bar signal() calls on a growing window.
    events: list[tuple[int, str, float | None]] = []
    vec_fn = getattr(setup, "vector_signals", None)
    if vec_fn is not None:
        try:
            dirs = np.asarray(vec_fn(df), dtype=float)
        except Exception:
            dirs = np.zeros(n)
        sl_arr = None
        stops_fn = getattr(setup, "vector_stops", None)
        if stops_fn is not None:
            try:
                stops_res = stops_fn(df)
                # vector_stops may legitimately return None (= use default SL);
                # np.asarray(None) would yield a 0-d NaN array, so guard first.
                if stops_res is not None:
                    sl_arr = np.asarray(stops_res, dtype=float)
                    if sl_arr.ndim != 1 or len(sl_arr) != n:
                        sl_arr = None
            except Exception:
                sl_arr = None
        lows  = df["low"].to_numpy(dtype=float)
        highs = df["high"].to_numpy(dtype=float)
        for i in np.nonzero(dirs != 0)[0]:
            if i < setup.min_periods or i >= n - MAX_HOLD_DAYS:
                continue
            direction = "sell" if dirs[i] < 0 else "buy"
            if sl_arr is not None:
                sl_val = sl_arr[i]
                sl = None if np.isnan(sl_val) else float(sl_val)
            else:
                # Mirrors BaseSetup.get_stoploss default: signal-bar low/high
                sl = float(lows[i]) if direction == "buy" else float(highs[i])
            events.append((int(i), direction, sl))
    else:
        for i in range(setup.min_periods, n - MAX_HOLD_DAYS, stride):
            sub_df = df.iloc[:i + 1]
            try:
                result = setup.signal(sub_df, symbol)
            except Exception:
                continue
            if not result.signal:
                continue
            direction = _direction_of(result.to_dict())
            try:
                sl_price = setup.get_stoploss(result, sub_df)
            except Exception:
                sl_price = None
            events.append((i, direction, sl_price))

    # ── Book every signal event ───────────────────────────────────────────────
    for i, direction, sl_price in events:
        entry_idx   = i + 1                              # D1
        entry_price = float(df["open"].iloc[entry_idx])  # D1 open is the entry
        if entry_price == 0:
            continue

        # Percentage-based SL override: replaces native SL when sl_pct is set.
        # sl_pct=2.0 → stop 2% below entry for longs, 2% above for shorts.
        if getattr(setup, 'sl_pct', None) is not None:
            pct = setup.sl_pct / 100.0
            if direction in ("sell", "short"):
                sl_price = entry_price * (1.0 + pct)
            else:
                sl_price = entry_price * (1.0 - pct)

        # ── Sell / short — intraday only → partial_short ─────────────────────
        if direction == "sell":
            d1_open  = float(df["open"].iloc[entry_idx])
            d1_high  = float(df["high"].iloc[entry_idx])
            d1_close = float(df["close"].iloc[entry_idx])

            if sl_price is not None and d1_open >= sl_price:
                continue   # D1 opened at/above SL — reversal already failed

            n_short += 1

            if sl_price is not None and d1_high >= sl_price:
                exit_price  = sl_price
                n_short_sl += 1
            else:
                exit_price  = d1_close

            ret = (entry_price - exit_price) / entry_price - cost
            _book(partial_short[1], "close", ret)
            continue

        # ── Long / neutral — multi-day hold → partial_long ────────────────────
        n_long += 1

        sl_hit_offset: int | None = None
        sl_exit_price: float      = 0.0
        if sl_price is not None:
            for offset in range(0, MAX_HOLD_DAYS):
                chk      = entry_idx + offset
                if chk >= n:
                    break
                bar_open = float(df["open"].iloc[chk])
                bar_low  = float(df["low"].iloc[chk])
                if bar_open <= sl_price:
                    sl_hit_offset = offset
                    sl_exit_price = bar_open
                    n_long_sl    += 1
                    break
                elif bar_low <= sl_price:
                    sl_hit_offset = offset
                    sl_exit_price = sl_price
                    n_long_sl    += 1
                    break

        sl_exit_d = sl_hit_offset + 1 if sl_hit_offset is not None else None

        for d in range(1, MAX_HOLD_DAYS + 1):
            exit_idx = entry_idx + (d - 1)
            if exit_idx >= n:
                break

            if sl_exit_d is not None and d >= sl_exit_d:
                sl_ret = (sl_exit_price - entry_price) / entry_price - cost
                if d == 1:
                    _book(partial_long[1], "close", sl_ret)
                else:
                    _book(partial_long[d], "open",  sl_ret)
                    _book(partial_long[d], "close", sl_ret)
            elif d == 1:
                ret = (float(df["close"].iloc[exit_idx]) - entry_price) / entry_price - cost
                _book(partial_long[1], "close", ret)
            else:
                for bkt, col in (("open", "open"), ("close", "close")):
                    ret = (float(df[col].iloc[exit_idx]) - entry_price) / entry_price - cost
                    _book(partial_long[d], bkt, ret)

    return partial_long, partial_short, n_long, n_short, n_long_sl, n_short_sl


def _summarise(by_day: dict, total_signals: int, total_sl_hits: int = 0) -> dict:
    """
    Convert raw by_day return sums + win counts to the final stats dict.

    All returns are already NET of transaction costs (subtracted in
    _process_one_stock), so every metric below reflects what is actually
    realisable, not a gross paper edge.

    Per-day stats:
      avg_return  = average net % return per trade = the EXPECTANCY (the single
                    most important figure: positive => the setup makes money on
                    average after costs, regardless of how often it wins).
      win_rate    = fraction of trades with net return > 0.
      confidence  = Bayesian win rate: (wins + PRIOR_N×PRIOR_WR) / (n + PRIOR_N).
                    Shrinks toward 50 % for small samples.
      wr_lower    = Wilson lower bound on the win rate at WR_CONFIDENCE — the
                    win rate we are confident the setup BEATS. Honest headline
                    reliability number: penalises small n and selection bias.
      ret_lower   = one-sided lower confidence bound on the mean net return —
                    the expectancy we are confident the setup beats. This is
                    what conviction weights are computed from, and what the
                    best-exit / best-day selection optimises (selecting on the
                    lower bound instead of the point estimate shrinks the
                    multiple-comparison optimism of trying 10 days × 2 exits).
      ret_std / t_stat = per-trade return dispersion and the t-statistic of
                    the mean — t < ~1.3 means the edge is not distinguishable
                    from noise at 90% confidence.
      avg_win/avg_loss/profit_factor = magnitude info a win rate hides. A high
                    win rate with a profit_factor < 1 is still a losing setup.
      best_exit   = 'close' for d=1 (only option); 'open' or 'close' for d≥2.
    """
    day_stats: dict[str, dict] = {}
    for d in range(1, MAX_HOLD_DAYS + 1):
        bd = by_day[d]
        o_sum = bd["open_ret_sum"];  o_n = bd["open_n"];  o_w = bd["open_wins"]
        c_sum = bd["close_ret_sum"]; c_n = bd["close_n"]; c_w = bd["close_wins"]
        o_sq  = bd.get("open_ret_sq",  0.0)
        c_sq  = bd.get("close_ret_sq", 0.0)

        avg_o = (o_sum / o_n) if o_n > 0 else None
        avg_c = (c_sum / c_n) if c_n > 0 else None
        wr_o  = (o_w / o_n)   if o_n > 0 else None
        wr_c  = (c_w / c_n)   if c_n > 0 else None

        lo_o = _mean_lower_bound(o_sum, o_sq, o_n)[0] if o_n > 1 else None
        lo_c = _mean_lower_bound(c_sum, c_sq, c_n)[0] if c_n > 1 else None

        # Best exit: whichever LOWER-BOUND net return is higher (conservative,
        # variance-aware choice; falls back to avg when variance is unknown).
        # For d=1 only close is populated (open_n=0 always).
        key_o = lo_o if lo_o is not None else avg_o
        key_c = lo_c if lo_c is not None else avg_c
        if key_o is not None and key_c is not None and key_o > key_c:
            best_avg, best_wr, best_n, best_w, best_exit = avg_o, wr_o, o_n, o_w, "open"
            best_sum, best_sq = o_sum, o_sq
        elif key_c is not None:
            best_avg, best_wr, best_n, best_w, best_exit = avg_c, wr_c, c_n, c_w, "close"
            best_sum, best_sq = c_sum, c_sq
        elif key_o is not None:
            best_avg, best_wr, best_n, best_w, best_exit = avg_o, wr_o, o_n, o_w, "open"
            best_sum, best_sq = o_sum, o_sq
        else:
            best_avg, best_wr, best_n, best_w, best_exit = 0.0, PRIOR_WR, 0, 0, "close"
            best_sum, best_sq = 0.0, 0.0

        # Bayesian confidence: smoothed win rate pulled toward 50 % with small n
        bw     = best_wr if best_wr is not None else PRIOR_WR
        bconf  = (bw * best_n + PRIOR_WR * PRIOR_N) / (best_n + PRIOR_N)
        # Wilson lower bound on the raw (unsmoothed) win rate of the best exit
        wr_lo  = _wilson_lower_bound(best_w, best_n)
        # Lower bound / dispersion / t-stat on the mean net return of best exit
        ret_lo, ret_std, t_stat = _mean_lower_bound(best_sum, best_sq, best_n)

        # Magnitude stats for the best exit (.get keeps old hand-built dicts safe)
        win_sum  = bd.get(f"{best_exit}_win_sum",  0.0)
        loss_sum = bd.get(f"{best_exit}_loss_sum", 0.0)
        loss_n   = bd.get(f"{best_exit}_loss_n",   0)
        avg_win  = (win_sum / best_w) if best_w  > 0 else 0.0
        avg_loss = (loss_sum / loss_n) if loss_n > 0 else 0.0
        prof_fac = (win_sum / abs(loss_sum)) if loss_sum < 0 else (
            float("inf") if win_sum > 0 else 0.0)

        day_stats[str(d)] = {
            "open_avg_return" : round(avg_o, 4) if avg_o is not None else None,
            "close_avg_return": round(avg_c, 4) if avg_c is not None else None,
            "open_win_rate"   : round(wr_o,  4) if wr_o  is not None else None,
            "close_win_rate"  : round(wr_c,  4) if wr_c  is not None else None,
            "avg_return"      : round(best_avg, 4),
            "win_rate"        : round(bw,    4),
            "confidence"      : round(bconf, 4),
            "wr_lower"        : round(wr_lo, 4),
            "ret_lower"       : round(ret_lo,  4),
            "ret_std"         : round(ret_std, 4),
            "t_stat"          : round(t_stat,  2),
            "avg_win"         : round(avg_win,  4),
            "avg_loss"        : round(avg_loss, 4),
            "profit_factor"   : round(prof_fac, 3) if prof_fac != float("inf") else None,
            "best_exit"       : best_exit,
            "open_n"          : o_n,
            "close_n"         : c_n,
            "tested"          : best_n,
        }

    valid = {
        d: day_stats[str(d)]
        for d in range(1, MAX_HOLD_DAYS + 1)
        if day_stats[str(d)]["tested"] >= MIN_OBS_FOR_BEST
    }
    if valid:
        # Select the holding day on the LOWER-BOUND expectancy, not the point
        # estimate — choosing the max of 10 in-sample averages systematically
        # overstates the edge; the lower bound self-corrects for noise.
        # Earlier day still preferred when within BEST_DAY_THRESHOLD of the
        # peak (shorter holds free up capital sooner).
        peak_lo = max(s["ret_lower"] for s in valid.values())
        best_d  = min(
            (d for d in valid if peak_lo - valid[d]["ret_lower"] <= BEST_DAY_THRESHOLD),
            key=lambda d: d,
        )
        bs = valid[best_d]
    else:
        best_d = 1
        bs     = day_stats["1"]

    best_avg  = bs["avg_return"]
    best_n    = bs["tested"]
    best_conf = bs["confidence"]
    best_wr   = bs["win_rate"]

    weight  = _compute_weight(best_avg, best_n, bs.get("ret_lower"))
    sl_rate = round(total_sl_hits / total_signals, 4) if total_signals > 0 else 0.0
    return {
        "total_signals"   : total_signals,
        "sl_hits"         : total_sl_hits,
        "sl_rate"         : sl_rate,
        "by_day"          : day_stats,
        "best_days"       : best_d,
        "best_avg_return" : round(best_avg,  4),
        "best_win_rate"   : round(best_wr,   4),
        "best_confidence" : round(best_conf, 4),
        "best_wr_lower"   : bs.get("wr_lower", 0.0),
        "best_ret_lower"  : bs.get("ret_lower", 0.0),
        "ret_lower"       : bs.get("ret_lower", 0.0),
        "ret_std"         : bs.get("ret_std", 0.0),
        "t_stat"          : bs.get("t_stat", 0.0),
        "avg_win"         : bs.get("avg_win",  0.0),
        "avg_loss"        : bs.get("avg_loss", 0.0),
        "profit_factor"   : bs.get("profit_factor"),
        "weight"          : weight,
        "avg_return"      : round(best_avg,  4),
        "win_rate"        : round(best_wr,   4),
        "confidence"      : round(best_conf, 4),
        "wr_lower"        : bs.get("wr_lower", 0.0),
        "tested"          : best_n,
        "sample_size"     : best_n,
    }


def _merge_direction_stats(long_stats: dict, short_stats: dict) -> dict:
    """
    Combine long and short stats into one result dict.

    Long stats  — multi-day hold performance (d=1..MAX_HOLD_DAYS).
    Short stats — intraday-only performance  (d=1 close only).

    Top-level fields expose the better direction so hyperparameter_search.py
    continues to work without modification (it reads best_avg_return, sl_rate,
    tested, etc. from the top level).

    Full per-direction data is available under the 'long' and 'short' keys.
    """
    lw = _compute_weight(long_stats["best_avg_return"],  long_stats["tested"],
                         long_stats.get("best_ret_lower"))
    sw = _compute_weight(short_stats["best_avg_return"], short_stats["tested"],
                         short_stats.get("best_ret_lower"))
    # A direction with zero observed signals carries no conviction — _compute_weight
    # returns the neutral 1.0 for n=0, which would otherwise let an empty book
    # inflate the overall weight above a losing one. Only let directions that
    # actually traded compete for the headline weight.
    candidates = []
    if long_stats["total_signals"]  > 0:
        candidates.append((lw, long_stats))
    if short_stats["total_signals"] > 0:
        candidates.append((sw, short_stats))
    if candidates:
        weight, best = max(candidates, key=lambda c: c[0])
    else:
        weight, best = 1.0, long_stats
    return {
        # ── Direction-specific sub-dicts (primary new data) ──────────────────
        "long"           : long_stats,
        "short"          : short_stats,
        "long_weight"    : lw,
        "short_weight"   : sw,
        # ── Top-level (best direction) — backward compat ─────────────────────
        "weight"         : weight,
        "avg_return"     : best["avg_return"],
        "win_rate"       : best["win_rate"],
        "confidence"     : best["confidence"],
        "best_avg_return": best["best_avg_return"],
        "best_win_rate"  : best["best_win_rate"],
        "best_confidence": best["best_confidence"],
        "best_wr_lower"  : best.get("best_wr_lower", 0.0),
        "wr_lower"       : best.get("wr_lower", 0.0),
        "best_ret_lower" : best.get("best_ret_lower", 0.0),
        "ret_lower"      : best.get("ret_lower", 0.0),
        "ret_std"        : best.get("ret_std", 0.0),
        "t_stat"         : best.get("t_stat", 0.0),
        "avg_win"        : best.get("avg_win",  0.0),
        "avg_loss"       : best.get("avg_loss", 0.0),
        "profit_factor"  : best.get("profit_factor"),
        "best_days"      : best["best_days"],
        "sl_rate"        : best["sl_rate"],
        "sl_hits"        : long_stats["sl_hits"] + short_stats["sl_hits"],
        "total_signals"  : long_stats["total_signals"] + short_stats["total_signals"],
        "tested"         : best["tested"],
        "sample_size"    : best["tested"],
        "by_day"         : best["by_day"],
    }


# ── Rolling multi-day backtest per setup ──────────────────────────────────────

def _backtest_setup(setup, stock_dfs: dict, stride: int = 2,
                    cost: float = TRANSACTION_COST) -> dict:
    """
    Run the full backtest for one setup — always sequential.

    Called by:
      • _worker_combo_task  — already running inside a worker process,
        parallelism is at the combination level (one process per combo).
      • hyperparameter_search.py — same reason.

    run_backtest() does NOT call this function; it uses the flat
    ProcessPoolExecutor pool directly (one process per stock × setup pair).

    `cost` defaults to the configured round-trip transaction cost so the
    hyperparameter search optimises NET returns, consistent with run_backtest().
    """
    by_day_long  = {d: _empty_day() for d in range(1, MAX_HOLD_DAYS + 1)}
    by_day_short = {d: _empty_day() for d in range(1, MAX_HOLD_DAYS + 1)}
    n_long = n_short = n_long_sl = n_short_sl = 0

    for symbol, df in stock_dfs.items():
        if len(df) < setup.min_periods + MAX_HOLD_DAYS + 1:
            continue
        pl, ps, nl, ns, nls, nss = _process_one_stock(setup, symbol, df, stride, cost)
        n_long    += nl;  n_short    += ns
        n_long_sl += nls; n_short_sl += nss
        for d in range(1, MAX_HOLD_DAYS + 1):
            for k in _DAY_KEYS:
                by_day_long[d][k]  += pl[d][k]
                by_day_short[d][k] += ps[d][k]

    return _merge_direction_stats(
        _summarise(by_day_long,  n_long,  n_long_sl),
        _summarise(by_day_short, n_short, n_short_sl),
    )


# ── Process-pool worker state and functions ───────────────────────────────────
# Defined AFTER _process_one_stock, _summarise, and _backtest_setup so every
# name they reference is already bound when the module is imported in a worker.
#
# On Windows, ProcessPoolExecutor uses the "spawn" start method — each worker
# starts a fresh Python interpreter and imports this module from scratch.
# Worker functions MUST be at module level (not closures or lambdas) so pickle
# can locate them by name.

_g_stock_dfs:   dict = {}   # populated by _worker_init; never written by main
_g_setup_cache: dict = {}   # setup instance cache; one dict per worker process


def _worker_init(serialized_stock_dfs: bytes) -> None:
    """
    Pool initialiser — called ONCE when each worker process starts.
    Deserialises the stock data into a module-level global so subsequent tasks
    can access it from memory without any further IPC overhead.
    """
    global _g_stock_dfs
    _g_stock_dfs = pickle.loads(serialized_stock_dfs)


def _load_setup_in_worker(setup_file: str, class_name: str, params: dict):
    """
    Load (or return cached) a setup instance inside a worker process.
    Imports the setup module from its original file path the first time;
    caches by (file, class, params) so each unique configuration is only
    constructed once per worker.
    """
    global _g_setup_cache
    cache_key = (setup_file, class_name, tuple(sorted(params.items())))
    if cache_key not in _g_setup_cache:
        mod_name = f"_bt_worker_{class_name}_{abs(hash(cache_key))}"
        spec = importlib.util.spec_from_file_location(mod_name, setup_file)
        mod  = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)          # type: ignore[attr-defined]
        cls = getattr(mod, class_name)
        # sl_pct is a meta-param not in any setup's constructor; extract & set post-init
        sl_pct       = params.get('sl_pct')
        setup_params = {k: v for k, v in params.items() if k != 'sl_pct'}
        instance     = cls(**setup_params)
        if sl_pct is not None:
            instance.sl_pct = sl_pct
        _g_setup_cache[cache_key] = instance
    return _g_setup_cache[cache_key]


def _worker_stock_task(
    setup_name: str,
    setup_file: str,
    class_name: str,
    params: dict,
    symbol: str,
    stride: int,
) -> tuple[str, dict, int, int]:
    """
    Worker function for run_backtest's flat (setup × stock) pool.
    Returns (setup_name, partial_by_day, n_signals, n_sl_hits).
    The main process aggregates the returned partial dicts.
    """
    _empty_long  = {d: _empty_day() for d in range(1, MAX_HOLD_DAYS + 1)}
    _empty_short = {d: _empty_day() for d in range(1, MAX_HOLD_DAYS + 1)}
    try:
        setup = _load_setup_in_worker(setup_file, class_name, params)
        df    = _g_stock_dfs.get(symbol)
        if df is None or len(df) < setup.min_periods + MAX_HOLD_DAYS + 1:
            return setup_name, _empty_long, _empty_short, 0, 0, 0, 0
        pl, ps, nl, ns, nls, nss = _process_one_stock(setup, symbol, df, stride,
                                                      TRANSACTION_COST)
        return setup_name, pl, ps, nl, ns, nls, nss
    except Exception:
        return setup_name, _empty_long, _empty_short, 0, 0, 0, 0


def _worker_combo_task(
    setup_file: str,
    class_name: str,
    params: dict,
    stride: int,
) -> dict:
    """
    Worker function for hyperparameter_search.py's combination pool.
    Runs the FULL sequential backtest for one parameter combination over all
    stocks in _g_stock_dfs.  Parallelism is at the combination level — one
    process per combo — so _backtest_setup is kept sequential here.
    """
    try:
        setup = _load_setup_in_worker(setup_file, class_name, params)
        return _backtest_setup(setup, _g_stock_dfs, stride)
    except Exception:
        return {"best_win_rate": 0.0, "tested": 0, "best_days": 1, "by_day": {}}


def _get_setup_params(setup) -> dict:
    """
    Extract the constructor parameters from a live setup instance by matching
    __init__ signature names to instance attributes.
    Also captures sl_pct if set (meta-param not in any constructor signature).
    """
    sig = inspect.signature(type(setup).__init__)
    params = {
        name: getattr(setup, name)
        for name, _ in sig.parameters.items()
        if name != "self" and hasattr(setup, name)
    }
    sl_pct = getattr(setup, 'sl_pct', None)
    if sl_pct is not None:
        params['sl_pct'] = sl_pct
    return params


# ── Public interface ──────────────────────────────────────────────────────────

def run_backtest(quick: bool = False, max_workers: int = _CPU_COUNT) -> dict[str, dict]:
    stride = 3 if quick else 2

    log.info("backtester: loading OHLCV from DB...")
    stocks    = get_active_stocks()
    stock_dfs = {}
    for s in stocks:
        df = get_ohlcv(s["symbol"], days=BACKTEST_WINDOW_DAYS)
        if not df.empty:
            stock_dfs[s["symbol"]] = df

    setups = load_setups()

    # ── Build flat task list: all eligible (setup, stock) pairs ──────────────
    # One task = one (setup, stock) pair.  All tasks go into a single
    # ProcessPoolExecutor so all CPUs are always busy — no idle workers
    # waiting while the slowest setup finishes.
    tasks: list[tuple] = []
    for setup in setups:
        setup_file = inspect.getfile(type(setup))
        class_name = type(setup).__name__
        params     = _get_setup_params(setup)
        for symbol, df in stock_dfs.items():
            if len(df) >= setup.min_periods + MAX_HOLD_DAYS + 1:
                tasks.append((setup.name, setup_file, class_name, params, symbol, stride))

    n_workers = min(max_workers, len(tasks))
    total     = len(tasks)
    log.info(
        f"backtester: {len(stock_dfs)} stocks, {len(setups)} setups, "
        f"{total} tasks, {n_workers} processes, stride={stride}"
    )

    # Serialise stock data once — transferred to each worker at pool startup,
    # NOT on every task call.
    log.info("backtester: serialising stock data for worker processes...")
    serialized_dfs = pickle.dumps(stock_dfs)
    log.info(f"backtester: {len(serialized_dfs) / 1e6:.1f} MB sent to workers")

    accum_long  = {s.name: {d: _empty_day() for d in range(1, MAX_HOLD_DAYS + 1)} for s in setups}
    accum_short = {s.name: {d: _empty_day() for d in range(1, MAX_HOLD_DAYS + 1)} for s in setups}
    sig_long  = {s.name: 0 for s in setups}
    sig_short = {s.name: 0 for s in setups}
    sl_long   = {s.name: 0 for s in setups}
    sl_short  = {s.name: 0 for s in setups}
    done    = 0
    t_start = time.time()

    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_worker_init,
        initargs=(serialized_dfs,),
    ) as pool:
        futures = {
            pool.submit(_worker_stock_task, *task): task[0]
            for task in tasks
        }
        for fut in as_completed(futures):
            sn, pl, ps, nl, ns, nls, nss = fut.result()
            sig_long[sn]  += nl;  sig_short[sn] += ns
            sl_long[sn]   += nls; sl_short[sn]  += nss
            for d in range(1, MAX_HOLD_DAYS + 1):
                for k in _DAY_KEYS:
                    accum_long[sn][d][k]  += pl[d][k]
                    accum_short[sn][d][k] += ps[d][k]
            done += 1
            if done % max(1, total // 20) == 0 or done == total:
                log.info(f"  {done}/{total} tasks done  ({time.time()-t_start:.0f}s)")

    results: dict[str, dict] = {}
    for setup in setups:
        long_s  = _summarise(accum_long[setup.name],  sig_long[setup.name],  sl_long[setup.name])
        short_s = _summarise(accum_short[setup.name], sig_short[setup.name], sl_short[setup.name])
        stats   = _merge_direction_stats(long_s, short_s)
        results[setup.name] = stats
        log.info(
            f"  {setup.name:<30} "
            f"LONG d{long_s['best_days']} {long_s['best_avg_return']:+.2%}/"
            f"{long_s['best_confidence']:.0%} SL={long_s['sl_rate']:.0%} "
            f"n={long_s['tested']:>4}  |  "
            f"SHORT {short_s['avg_return']:+.2%}/{short_s['confidence']:.0%} "
            f"SL={short_s['sl_rate']:.0%} n={short_s['total_signals']:>4}  |  "
            f"w={stats['weight']:.3f}"
        )
    return results


def save_weights(results: dict) -> None:
    payload = {
        "generated_at": datetime.now().isoformat(),
        "setups": {
            name: {
                # Direction-specific sub-dicts (primary data)
                "long"            : s.get("long", {}),
                "short"           : s.get("short", {}),
                "long_weight"     : s.get("long_weight", s["weight"]),
                "short_weight"    : s.get("short_weight", s["weight"]),
                # Top-level (best direction) — for backward compat
                "weight"          : s["weight"],
                "avg_return"      : s["avg_return"],
                "win_rate"        : s["win_rate"],
                "confidence"      : s["confidence"],
                "sample_size"     : s["tested"],
                "best_days"       : s["best_days"],
                "best_avg_return" : s["best_avg_return"],
                "best_win_rate"   : s["best_win_rate"],
                "best_confidence" : s["best_confidence"],
                # Honest, after-cost reliability + magnitude metrics
                "wr_lower"        : s.get("wr_lower", 0.0),
                "best_wr_lower"   : s.get("best_wr_lower", 0.0),
                "best_ret_lower"  : s.get("best_ret_lower", 0.0),
                "ret_lower"       : s.get("ret_lower", 0.0),
                "ret_std"         : s.get("ret_std", 0.0),
                "t_stat"          : s.get("t_stat", 0.0),
                "avg_win"         : s.get("avg_win",  0.0),
                "avg_loss"        : s.get("avg_loss", 0.0),
                "profit_factor"   : s.get("profit_factor"),
                "sl_hits"         : s.get("sl_hits", 0),
                "sl_rate"         : s.get("sl_rate", 0.0),
                "by_day"          : s["by_day"],
            }
            for name, s in results.items()
        },
    }
    WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    WEIGHTS_PATH.write_text(json.dumps(payload, indent=2))
    log.info(f"backtester: weights written to {WEIGHTS_PATH}")


def load_weights() -> dict[str, float]:
    """Return {setup_name: overall_weight}. Empty dict if file missing."""
    if not WEIGHTS_PATH.exists():
        return {}
    try:
        data = json.loads(WEIGHTS_PATH.read_text())
        return {name: info["weight"] for name, info in data.get("setups", {}).items()}
    except Exception:
        return {}


def load_directional_weights() -> dict[str, dict]:
    """
    Return {setup_name: {"long": float, "short": float, "overall": float}}.
    Falls back to overall weight when direction-specific weights are absent
    (e.g. old JSON files written before the direction split).
    """
    if not WEIGHTS_PATH.exists():
        return {}
    try:
        data = json.loads(WEIGHTS_PATH.read_text())
        result = {}
        for name, info in data.get("setups", {}).items():
            w = info.get("weight", 1.0)
            result[name] = {
                "long"   : info.get("long_weight",  w),
                "short"  : info.get("short_weight", w),
                "overall": w,
            }
        return result
    except Exception:
        return {}


def load_stats() -> dict[str, dict]:
    """
    Return full stats per setup including by_day breakdown.
    Empty dict if backtester hasn't been run yet.
    """
    if not WEIGHTS_PATH.exists():
        return {}
    try:
        data = json.loads(WEIGHTS_PATH.read_text())
        return data.get("setups", {})
    except Exception:
        return {}


# ── CLI entry point ───────────────────────────────────────────────────────────

def _parse_int_arg(flag: str, default: int) -> int:
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                pass
    return default


if __name__ == "__main__":
    quick       = "--quick" in sys.argv
    max_workers = _parse_int_arg("--workers", _CPU_COUNT)

    print("\n" + "=" * 70)
    print("  Signal Infomer — Strategy Backtester")
    print(f"  Testing exit days 1–{MAX_HOLD_DAYS} after each signal")
    print(f"  Workers: {max_workers} processes  (CPU count={_CPU_COUNT})")
    if quick:
        print("  (quick mode: stride=3)")
    print("=" * 70 + "\n")

    t_start = time.time()
    results = run_backtest(quick=quick, max_workers=max_workers)
    elapsed = time.time() - t_start

    # Print direction-separated table:
    # LONG: best holding day + confidence + SL rate | SHORT: D1 stats | Weight
    hdr = (f"{'Setup':<32}  {'LONG: best(Avg/Conf/SL)':>24}  {'n_long':>7}  "
           f"{'SHORT: D1(Avg/Conf/SL)':>23}  {'n_short':>8}  {'Weight':>7}")
    print(hdr)
    print("-" * len(hdr))
    for name, s in sorted(results.items(), key=lambda x: -x[1]["weight"]):
        ls = s.get("long",  s)
        ss = s.get("short", s)
        long_str  = (f"d{ls.get('best_days',1)} "
                     f"{ls.get('best_avg_return',0):+.1%}/"
                     f"{ls.get('best_confidence',0.5):.0%}/"
                     f"{ls.get('sl_rate',0):.0%}")
        short_str = (f"{ss.get('avg_return',0):+.1%}/"
                     f"{ss.get('confidence',0.5):.0%}/"
                     f"{ss.get('sl_rate',0):.0%}")
        print(f"{name:<32}  {long_str:>24}  {ls.get('tested',0):>7}  "
              f"{short_str:>23}  {ss.get('total_signals',0):>8}  {s['weight']:>6.3f}")

    print(f"\nTotal time: {elapsed:.0f}s")
    save_weights(results)
    print(f"Weights saved to {WEIGHTS_PATH}\n")
