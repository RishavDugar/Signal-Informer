"""
Signal Infomer — HFT / Intraday Backtester

Runs every setup that exposes vector_signals() on stored intraday parquet
data (1min / 5min / 10min / 15min buckets under data/hft/).

Trading rules (differ from the daily engine):
  • INTRADAY ONLY — every position is closed at the last bar of its session.
    No overnight risk, so longs AND shorts are both allowed.
  • Entry at the NEXT bar's open after the signal bar (no look-ahead).
  • A signal on the last bar of a session is discarded (nothing left to trade).
  • Optional percentage stop-loss (sl_pct): checked intrabar on every bar from
    entry to session end. Gap-through opens fill at the open (worst case).
  • Costs: HFT_TRANSACTION_COST round trip — intraday costs in Indian equities
    are far below delivery costs (no delivery STT; brokerage capped; tighter
    spreads on NSE large caps), default 10 bps vs 30 bps for delivery.
  • Screens: HFT_MIN_AVG_RETURN (default 5 bps) — relaxed vs the daily 50 bps
    because intraday edges are smaller per trade but recur many times per day.

Statistics per (setup × timeframe), all net of costs:
  n, win rate + Wilson lower bound, avg net return, std, t-stat,
  ret_lower (lower confidence bound on the mean), profit factor,
  avg win / avg loss, SL hit rate, avg holding bars, long/short split.

Results → db/hft_results.json (+ console table).

Usage:
    python hft_backtester.py --timeframes 15min,5min --years 2024,2025,2026
    python hft_backtester.py --timeframes 1min --symbols 50 --years 2025,2026
    python hft_backtester.py --setup RSI2_EXTREME --timeframes 15min
"""

from __future__ import annotations

import gc
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config import BASE_DIR, WR_CONFIDENCE
from utils.logger import get_logger

log = get_logger("hft_backtester")

HFT_DATA_DIR     = BASE_DIR / "data" / "hft"
HFT_RESULTS_PATH = BASE_DIR / "db" / "hft_results.json"

# Round-trip intraday cost (fraction of notional). Indian equity intraday:
# brokerage ~0-40Rs/order capped, STT 0.025% sell side only, exchange+SEBI+GST
# small, slippage ~2-5 bps/side on NSE-500 liquid names. 10 bps round trip is
# a realistic blended figure for limit-ish execution; tune per broker.
HFT_TRANSACTION_COST = float(os.getenv("HFT_TRANSACTION_COST", "0.0010"))
# Relaxed minimum average net return per trade for HFT signals (5 bps).
HFT_MIN_AVG_RETURN   = float(os.getenv("HFT_MIN_AVG_RETURN", "0.0005"))
MIN_TRADES           = 30      # below this a (setup, timeframe) row is noise

_Z_BY_CONF = {0.80: 0.8416, 0.85: 1.0364, 0.90: 1.2816, 0.95: 1.6449, 0.975: 1.9600}
_Z = _Z_BY_CONF.get(round(WR_CONFIDENCE, 3), 1.2816)

_CPU = os.cpu_count() or 4


# ── Data loading ──────────────────────────────────────────────────────────────

def list_symbols(timeframe: str) -> list[str]:
    tf_dir = HFT_DATA_DIR / f"timeframe={timeframe}"
    if not tf_dir.exists():
        return []
    return sorted(p.name.split("=", 1)[1] for p in tf_dir.iterdir()
                  if p.is_dir() and p.name.startswith("symbol="))


def load_symbol(timeframe: str, symbol: str,
                years: list[int] | None = None) -> pd.DataFrame:
    """Load one symbol's intraday bars, optionally restricted to years.
    Returns a DataFrame indexed by timestamp with a `session` (date) column."""
    sym_dir = HFT_DATA_DIR / f"timeframe={timeframe}" / f"symbol={symbol}"
    if not sym_dir.exists():
        return pd.DataFrame()
    parts = []
    for ydir in sorted(sym_dir.iterdir()):
        if not ydir.name.startswith("year="):
            continue
        y = int(ydir.name.split("=", 1)[1])
        if years and y not in years:
            continue
        try:
            parts.append(pd.read_parquet(
                ydir, columns=["timestamp", "open", "high", "low", "close", "volume"]))
        except Exception as exc:
            log.warning(f"hft: cannot read {ydir} — {exc}")
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    df = df.sort_values("timestamp").set_index("timestamp")
    df = df[~df.index.duplicated(keep="first")]
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype("float32")
    df["volume"] = df["volume"].astype("int32")
    df["session"] = df.index.normalize()
    return df


# ── Core engine ───────────────────────────────────────────────────────────────

def _trades_for_symbol(setup, df: pd.DataFrame,
                       cost: float) -> tuple[list[tuple], int]:
    """
    Generate intraday trades for one (setup, symbol).
    Returns (trades, n_sl_hits); each trade = (net_ret, direction, hold_bars).

    Entry  : next bar open after the signal bar (same session only).
    Exit   : sl_pct stop intrabar if armed, else the session's last bar close.
    """
    n = len(df)
    if n < setup.min_periods + 2:
        return [], 0

    try:
        dirs = np.asarray(setup.vector_signals(df), dtype=float)
    except Exception:
        return [], 0

    sig_idx = np.nonzero(dirs != 0)[0]
    if len(sig_idx) == 0:
        return [], 0

    opens   = df["open"].to_numpy(dtype=float)
    highs   = df["high"].to_numpy(dtype=float)
    lows    = df["low"].to_numpy(dtype=float)
    closes  = df["close"].to_numpy(dtype=float)
    session = df["session"].to_numpy()

    # last bar index of each session, aligned per bar
    sess_change = np.empty(n, dtype=bool)
    sess_change[:-1] = session[:-1] != session[1:]
    sess_change[-1]  = True
    last_of_session  = np.where(sess_change)[0]
    # for bar i: index of its session's last bar
    sess_end = last_of_session[np.searchsorted(last_of_session, np.arange(n))]

    sl_pct = getattr(setup, "sl_pct", None)
    trades: list[tuple] = []
    n_sl = 0

    for i in sig_idx:
        if i < setup.min_periods:
            continue
        entry_idx = i + 1
        if entry_idx >= n or session[entry_idx] != session[i]:
            continue          # signal on the session's last bar — untradeable
        entry = opens[entry_idx]
        if entry <= 0 or np.isnan(entry):
            continue
        # ── Executable-fill guards ────────────────────────────────────────────
        # Circuit / halt regimes print bars you cannot actually trade:
        #  • a >10% bar-to-bar move puts the stock in the exchange's circuit
        #    band — the printed next-bar open is not a fillable price;
        #  • an entry bar with zero range (high == low) is locked at a circuit
        #    limit — there is no liquidity at the print.
        prev_c = closes[i - 1] if i > 0 else closes[i]
        if prev_c > 0 and abs(closes[i] / prev_c - 1.0) > 0.10:
            continue
        if highs[entry_idx] <= lows[entry_idx]:
            continue
        end_idx   = sess_end[entry_idx]
        direction = 1 if dirs[i] > 0 else -1

        exit_price = closes[end_idx]
        exit_idx   = end_idx
        if sl_pct is not None:
            pct = sl_pct / 100.0
            sl  = entry * (1.0 - pct) if direction == 1 else entry * (1.0 + pct)
            o_seg = opens[entry_idx:end_idx + 1]
            if direction == 1:
                x_seg    = lows[entry_idx:end_idx + 1]
                open_hit = o_seg <= sl
                bar_hit  = x_seg <= sl
            else:
                x_seg    = highs[entry_idx:end_idx + 1]
                open_hit = o_seg >= sl
                bar_hit  = x_seg >= sl
            any_hit = open_hit | bar_hit
            if any_hit.any():
                j_rel      = int(np.argmax(any_hit))
                exit_idx   = entry_idx + j_rel
                # gap-through open fills at the open (worse), else at the stop
                exit_price = o_seg[j_rel] if open_hit[j_rel] else sl
                n_sl      += 1

        gross = (exit_price - entry) / entry * direction
        trades.append((gross - cost, direction, exit_idx - entry_idx + 1))

    return trades, n_sl


def _summarise_trades(trades: list[tuple], n_sl: int) -> dict:
    """Aggregate trade tuples into the stats dict (all net of costs)."""
    if not trades:
        return {"n": 0}
    rets  = np.array([t[0] for t in trades])
    dirs  = np.array([t[1] for t in trades])
    holds = np.array([t[2] for t in trades])
    n     = len(rets)
    wins  = int((rets > 0).sum())
    mean  = float(rets.mean())
    std   = float(rets.std(ddof=1)) if n > 1 else 0.0
    se    = std / np.sqrt(n) if n > 1 and std > 0 else 0.0
    t_st  = mean / se if se > 0 else 0.0
    lo    = mean - _Z * se if se > 0 else 0.0
    # Wilson lower bound on win rate
    p, z2 = wins / n, _Z * _Z
    wr_lo = max(0.0, ((p + z2 / (2 * n))
                      - _Z * np.sqrt((p * (1 - p) + z2 / (4 * n)) / n))
                / (1 + z2 / n))
    win_sum  = float(rets[rets > 0].sum())
    loss_sum = float(rets[rets < 0].sum())
    return {
        "n"            : n,
        "n_long"       : int((dirs == 1).sum()),
        "n_short"      : int((dirs == -1).sum()),
        "win_rate"     : round(wins / n, 4),
        "wr_lower"     : round(float(wr_lo), 4),
        "avg_return"   : round(mean, 5),
        "ret_std"      : round(std, 5),
        "t_stat"       : round(float(t_st), 2),
        "ret_lower"    : round(float(lo), 5),
        "avg_win"      : round(win_sum / wins, 5) if wins else 0.0,
        "avg_loss"     : round(loss_sum / (n - wins), 5) if n > wins else 0.0,
        "profit_factor": round(win_sum / abs(loss_sum), 3) if loss_sum < 0 else None,
        "long_avg"     : round(float(rets[dirs == 1].mean()), 5)  if (dirs == 1).any()  else None,
        "short_avg"    : round(float(rets[dirs == -1].mean()), 5) if (dirs == -1).any() else None,
        "sl_hits"      : n_sl,
        "sl_rate"      : round(n_sl / n, 4),
        "avg_hold_bars": round(float(holds.mean()), 1),
        "passes_screen": bool(mean >= HFT_MIN_AVG_RETURN and lo > 0),
    }


# ── Worker plumbing (spawn-safe, module level) ────────────────────────────────

_gw_setups: list = []
_gw_timeframe = ""
_gw_years: list[int] | None = None


def _hft_worker_init(timeframe: str, years: list[int] | None,
                     use_optimal: bool) -> None:
    """Load all vector-capable setups once per worker process."""
    global _gw_setups, _gw_timeframe, _gw_years
    from setup_loader import load_setups
    _gw_setups    = [s for s in load_setups(use_optimal_params=use_optimal)
                     if hasattr(s, "vector_signals")]
    _gw_timeframe = timeframe
    _gw_years     = years


def _hft_worker_symbol(symbol: str) -> dict[str, tuple[list[tuple], int]]:
    """Run every setup over one symbol's intraday data. Returns
    {setup_name: (trades, n_sl)}."""
    out: dict[str, tuple[list[tuple], int]] = {}
    try:
        df = load_symbol(_gw_timeframe, symbol, _gw_years)
    except Exception:
        return out
    if df.empty:
        return out
    for setup in _gw_setups:
        try:
            trades, n_sl = _trades_for_symbol(setup, df, HFT_TRANSACTION_COST)
        except Exception:
            continue
        if trades:
            out[setup.name] = (trades, n_sl)
    del df
    gc.collect()
    return out


# ── Public interface ──────────────────────────────────────────────────────────

def run_hft_backtest(timeframe: str,
                     years: list[int] | None = None,
                     max_symbols: int | None = None,
                     max_workers: int = _CPU,
                     use_optimal: bool = True,
                     target_setup: str | None = None) -> dict[str, dict]:
    symbols = list_symbols(timeframe)
    if max_symbols:
        symbols = symbols[:max_symbols]
    if not symbols:
        log.error(f"hft: no symbols found for timeframe={timeframe}")
        return {}

    log.info(f"hft[{timeframe}]: {len(symbols)} symbols, years={years or 'all'}, "
             f"{max_workers} workers, cost={HFT_TRANSACTION_COST:.4%}")

    all_trades: dict[str, list[tuple]] = {}
    all_sl:     dict[str, int]         = {}
    done, t0 = 0, time.time()

    with ProcessPoolExecutor(
        max_workers=max_workers,
        initializer=_hft_worker_init,
        initargs=(timeframe, years, use_optimal),
        max_tasks_per_child=15,
    ) as pool:
        futures = {pool.submit(_hft_worker_symbol, s): s for s in symbols}
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception as exc:
                log.warning(f"hft: worker failed on {futures[fut]} — {exc}")
                res = {}
            for name, (trades, n_sl) in res.items():
                if target_setup and name != target_setup:
                    continue
                all_trades.setdefault(name, []).extend(trades)
                all_sl[name] = all_sl.get(name, 0) + n_sl
            done += 1
            if done % max(1, len(symbols) // 10) == 0 or done == len(symbols):
                log.info(f"  [{timeframe}] {done}/{len(symbols)} symbols "
                         f"({time.time()-t0:.0f}s)")

    results = {}
    for name, trades in all_trades.items():
        stats = _summarise_trades(trades, all_sl.get(name, 0))
        if stats.get("n", 0) >= MIN_TRADES:
            results[name] = stats
    return results


def save_hft_results(by_timeframe: dict[str, dict], meta: dict) -> None:
    existing = {}
    if HFT_RESULTS_PATH.exists():
        try:
            existing = json.loads(HFT_RESULTS_PATH.read_text()).get("timeframes", {})
        except Exception:
            pass
    existing.update(by_timeframe)
    payload = {
        "generated_at": datetime.now().isoformat(),
        "cost"        : HFT_TRANSACTION_COST,
        "min_avg_ret" : HFT_MIN_AVG_RETURN,
        "meta"        : meta,
        "timeframes"  : existing,
    }
    HFT_RESULTS_PATH.write_text(json.dumps(payload, indent=2))
    log.info(f"hft: results written to {HFT_RESULTS_PATH}")


def _print_table(timeframe: str, results: dict[str, dict], top: int = 40) -> None:
    print(f"\n=== {timeframe} -- top setups by ret_lower (net of "
          f"{HFT_TRANSACTION_COST:.2%} cost) ===")
    hdr = (f"{'Setup':<26} {'n':>7} {'L/S':>11} {'WR':>6} {'WRlo':>6} "
           f"{'Avg':>8} {'RetLo':>8} {'t':>6} {'PF':>6} {'Hold':>5} {'OK':>3}")
    print(hdr); print("-" * len(hdr))
    rows = sorted(results.items(), key=lambda kv: -(kv[1].get("ret_lower") or -9))
    for name, s in rows[:top]:
        print(f"{name:<26} {s['n']:>7} {str(s['n_long'])+'/'+str(s['n_short']):>11} "
              f"{s['win_rate']:>6.1%} {s['wr_lower']:>6.1%} "
              f"{s['avg_return']:>8.4%} {s['ret_lower']:>8.4%} "
              f"{s['t_stat']:>6.1f} {str(s['profit_factor']):>6} "
              f"{s['avg_hold_bars']:>5} {'Y' if s['passes_screen'] else '':>3}")


def _parse_list(flag: str, default: str) -> list[str]:
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return [x.strip() for x in sys.argv[i + 1].split(",") if x.strip()]
    return [x for x in default.split(",") if x]


def _parse_int(flag: str, default):
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                pass
    return default


if __name__ == "__main__":
    timeframes  = _parse_list("--timeframes", "15min")
    years_raw   = _parse_list("--years", "")
    years       = [int(y) for y in years_raw] or None
    max_symbols = _parse_int("--symbols", None)
    max_workers = _parse_int("--workers", _CPU)
    target      = None
    for i, a in enumerate(sys.argv):
        if a == "--setup" and i + 1 < len(sys.argv):
            target = sys.argv[i + 1]

    print("=" * 78)
    print("  Signal Infomer — HFT / Intraday Backtester")
    print(f"  Timeframes: {timeframes}  years={years or 'all'}  "
          f"symbols={max_symbols or 'all'}")
    print(f"  Cost {HFT_TRANSACTION_COST:.2%} round trip | screen: avg >= "
          f"{HFT_MIN_AVG_RETURN:.3%} & ret_lower > 0 | long+short | EOD square-off")
    print("=" * 78)

    out: dict[str, dict] = {}
    for tf in timeframes:
        res = run_hft_backtest(tf, years=years, max_symbols=max_symbols,
                               max_workers=max_workers, target_setup=target)
        out[tf] = res
        _print_table(tf, res)

    save_hft_results(out, {
        "years": years, "max_symbols": max_symbols,
        "timeframes_run": timeframes,
    })
    print(f"\nSaved to {HFT_RESULTS_PATH}\n")
