"""
Signal Infomer — Hyperparameter Search

Two search modes
----------------
Grid search (default)
    Exhaustive search over a fixed set of parameter values.
    Use as the first pass to understand the parameter landscape.

    python hyperparameter_search.py
    python hyperparameter_search.py --quick
    python hyperparameter_search.py --setup RSI_EXTREME

Random search  (--random)
    Samples the parameter space randomly over a wider range.
    Finds multiple LOCAL MAXIMA — distinct peaks that a fixed grid can miss.
    Each peak is validated at full backtester precision.

    python hyperparameter_search.py --random
    python hyperparameter_search.py --random --samples 150
    python hyperparameter_search.py --random --peaks 5
    python hyperparameter_search.py --random --setup HOLY_GRAIL

Both modes
    python hyperparameter_search.py --no-validate       skip validation (faster)
    python hyperparameter_search.py --force-rerun       re-tune all setups even if already tuned
    python hyperparameter_search.py --setup NAME        run only this setup (skips if tuned; add --force-rerun to override)

Output
------
  db/optimal_params.json   best params per setup (read by setup_loader.py)
  Console table            per-setup WR breakdown
"""

from __future__ import annotations

import importlib.util
import inspect
import itertools
import json
import os
import pickle
import random as _random
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from backtester import (
    _backtest_setup, WEIGHTS_PATH,
    _worker_init, _worker_combo_task,   # shared process-pool workers
)
from config import BACKTEST_WINDOW_DAYS
from core.base_setup import BaseSetup
from data.db import get_active_stocks, get_ohlcv
from utils.logger import get_logger

log = get_logger("hypersearch")

OPTIMAL_PARAMS_PATH = WEIGHTS_PATH.parent / "optimal_params.json"
_SETUP_DIR          = Path(__file__).parent / "Trading Setups"

# Parallelism: default to all logical CPUs. Override with --workers N.
# ThreadPoolExecutor is used (not ProcessPoolExecutor) because:
#   • dynamically-loaded setup classes are not picklable across processes
#   • pandas/numpy operations release the GIL, so threads get real parallelism
_CPU_COUNT = os.cpu_count() or 4

# Minimum observations for a parameter combo to be considered valid.
# Stricter than the backtester (5) to avoid over-fitting noisy combos.
MIN_OBS = 30


def _combined_score(avg_return: float, sl_rate: float) -> float:
    """
    Combined hyperparameter selection score.

    Rewards high avg return while penalising frequent stop-outs.
    For winning strategies: effective return = avg_return × (1 − sl_rate × 0.5)
      e.g. avg=+2%, sl=70% → 2% × 0.65 = 1.30%
           avg=+2%, sl=10% → 2% × 0.95 = 1.90%   ← preferred
    Losing strategies score as-is (excluded by MIN_AVG_RETURN filter anyway).
    """
    if avg_return <= 0.0:
        return avg_return
    return avg_return * (1.0 - sl_rate * 0.5)

# ── Parameter grids ────────────────────────────────────────────────────────────
# Each entry: {param_name: [values_to_try]}.
# Book-specified defaults are always included in the grid.

_SL_PCT_GRID  = [1.0, 2.0, 3.0, 5.0]   # % of entry price (longs: below; shorts: above)

PARAM_GRIDS: dict[str, dict[str, list]] = {
    "RSI_EXTREME": {
        "period"    : [7, 9, 14, 21],
        "overbought": [65, 70, 75, 80],
        "oversold"  : [20, 25, 30, 35],
        "sl_pct"    : _SL_PCT_GRID,
    },
    "TURTLE_SOUP": {
        "lookback"    : [15, 20, 25],
        "min_sessions": [3, 4, 5, 6],
        "sl_pct"      : _SL_PCT_GRID,
    },
    "TURTLE_SOUP_PLUS_ONE": {
        "lookback"    : [15, 20, 25],
        "min_sessions": [2, 3, 4],
        "sl_pct"      : _SL_PCT_GRID,
    },
    "EIGHTY_TWENTY": {
        "threshold": [0.10, 0.15, 0.20, 0.25, 0.30],
        "sl_pct"   : _SL_PCT_GRID,
    },
    "MOMENTUM_PINBALL": {
        "rsi_period": [2, 3, 4, 5],
        "oversold"  : [20, 25, 30],
        "overbought": [70, 75, 80],
        "sl_pct"    : _SL_PCT_GRID,
    },
    "THE_ANTI": {
        "k_period": [5, 7, 9, 14],
        "d_period": [7, 10, 14, 20],
        "sl_pct"  : _SL_PCT_GRID,
    },
    "HOLY_GRAIL": {
        "adx_period"   : [10, 12, 14, 18],
        "ema_period"   : [14, 20, 25],
        "adx_threshold": [25, 30, 35],
        "sl_pct"       : _SL_PCT_GRID,
    },
    "ADX_GAPPER": {
        "adx_period"   : [10, 12, 14],
        "di_period"    : [21, 28, 35],
        "adx_threshold": [25, 30, 35],
        "sl_pct"       : _SL_PCT_GRID,
    },
    "HV_NR4": {
        "short_period"      : [4, 5, 6, 7],
        "long_period"       : [50, 100, 150],
        "hv_ratio_threshold": [0.30, 0.40, 0.50, 0.60],
        "sl_pct"            : _SL_PCT_GRID,
    },
    "ID_NR4": {
        "require_both": [True, False],
        "sl_pct"      : _SL_PCT_GRID,
    },
    "MACD_DIVERGENCE": {
        "fast_period"  : [8, 12, 16],
        "slow_period"  : [21, 26, 30],
        "signal_period": [7,  9, 12],
        "lookback"     : [10, 15, 20],
        "sl_pct"       : _SL_PCT_GRID,
    },
    "BOLLINGER_SQUEEZE": {
        "period"          : [15, 20, 25],
        "num_stddev"      : [1.5, 2.0, 2.5],
        "squeeze_lookback": [10, 20, 30],
        "sl_pct"          : _SL_PCT_GRID,
    },
    "EMA_TREND_PULLBACK": {
        "fast_ema": [20, 50],
        "slow_ema": [100, 150, 200],
        "lookback": [3, 5, 8],
        "sl_pct"  : _SL_PCT_GRID,
    },
    "N_DOWN_REVERSAL": {
        "n_down"      : [3, 4, 5],
        "trend_period": [50, 100, 200],
        "sl_pct"      : _SL_PCT_GRID,
    },
    "VOLUME_CLIMAX": {
        "vol_period"     : [15, 20],
        "vol_multiplier" : [1.5, 2.0, 2.5],
        "atr_period"     : [10, 14],
        "atr_multiplier" : [1.5, 2.0],
        "close_threshold": [0.20, 0.25],
        "sl_pct"         : _SL_PCT_GRID,
    },
    # Previously untuned (book-specified rules) — now SL distance is tunable
    "TWO_PERIOD_ROC"        : {"sl_pct": _SL_PCT_GRID},
    "WHIPLASH"              : {"sl_pct": _SL_PCT_GRID},
    "THREE_DAY_GAP_REVERSAL": {"sl_pct": _SL_PCT_GRID},
}

_SL_PCT_QUICK = [1.0, 3.0]   # quick mode: 2 SL levels

# Reduced grids for --quick mode (2 values per param instead of 3-4)
_QUICK_GRIDS: dict[str, dict[str, list]] = {
    "RSI_EXTREME"          : {"period": [9, 14], "overbought": [70, 75], "oversold": [25, 30], "sl_pct": _SL_PCT_QUICK},
    "TURTLE_SOUP"          : {"lookback": [20, 25], "min_sessions": [4, 5], "sl_pct": _SL_PCT_QUICK},
    "TURTLE_SOUP_PLUS_ONE" : {"lookback": [20, 25], "min_sessions": [3, 4], "sl_pct": _SL_PCT_QUICK},
    "EIGHTY_TWENTY"        : {"threshold": [0.15, 0.20], "sl_pct": _SL_PCT_QUICK},
    "MOMENTUM_PINBALL"     : {"rsi_period": [3, 4], "oversold": [25, 30], "overbought": [70, 75], "sl_pct": _SL_PCT_QUICK},
    "THE_ANTI"             : {"k_period": [7, 9], "d_period": [10, 14], "sl_pct": _SL_PCT_QUICK},
    "HOLY_GRAIL"           : {"adx_period": [12, 14], "ema_period": [20, 25], "adx_threshold": [25, 30], "sl_pct": _SL_PCT_QUICK},
    "ADX_GAPPER"           : {"adx_period": [10, 12], "di_period": [28, 35], "adx_threshold": [25, 30], "sl_pct": _SL_PCT_QUICK},
    "HV_NR4"               : {"short_period": [5, 6], "long_period": [100, 150], "hv_ratio_threshold": [0.40, 0.50], "sl_pct": _SL_PCT_QUICK},
    "ID_NR4"               : {"require_both": [True, False], "sl_pct": _SL_PCT_QUICK},
    "MACD_DIVERGENCE"      : {"fast_period": [8, 12], "slow_period": [21, 26], "signal_period": [7, 9], "lookback": [10, 15], "sl_pct": _SL_PCT_QUICK},
    "BOLLINGER_SQUEEZE"    : {"period": [15, 20], "num_stddev": [2.0, 2.5], "squeeze_lookback": [15, 20], "sl_pct": _SL_PCT_QUICK},
    "EMA_TREND_PULLBACK"   : {"fast_ema": [20, 50], "slow_ema": [100, 200], "lookback": [3, 5], "sl_pct": _SL_PCT_QUICK},
    "N_DOWN_REVERSAL"      : {"n_down": [3, 4], "trend_period": [50, 100], "sl_pct": _SL_PCT_QUICK},
    "VOLUME_CLIMAX"        : {"vol_period": [15, 20], "vol_multiplier": [1.5, 2.0], "atr_period": [10, 14], "atr_multiplier": [1.5, 2.0], "close_threshold": [0.20, 0.25], "sl_pct": _SL_PCT_QUICK},
    "TWO_PERIOD_ROC"        : {"sl_pct": _SL_PCT_QUICK},
    "WHIPLASH"              : {"sl_pct": _SL_PCT_QUICK},
    "THREE_DAY_GAP_REVERSAL": {"sl_pct": _SL_PCT_QUICK},
}

# ── Random-search parameter ranges ────────────────────────────────────────────
# Wider than PARAM_GRIDS; the random sampler draws uniformly from [lo, hi].
# type=int  → sampled as integers
# type=float→ sampled as floats, rounded to 2 dp
# type=bool → sampled as True / False

_SL_PCT_RANGE = {"type": float, "lo": 0.5, "hi": 8.0}   # % of entry price

PARAM_RANGES_RANDOM: dict[str, dict[str, dict]] = {
    "RSI_EXTREME": {
        "period"    : {"type": int,   "lo": 3,    "hi": 30  },
        "overbought": {"type": float, "lo": 55.0, "hi": 90.0},
        "oversold"  : {"type": float, "lo": 5.0,  "hi": 45.0},
        "sl_pct"    : _SL_PCT_RANGE,
    },
    "TURTLE_SOUP": {
        "lookback"    : {"type": int, "lo": 10, "hi": 30},
        "min_sessions": {"type": int, "lo": 2,  "hi": 9 },
        "sl_pct"      : _SL_PCT_RANGE,
    },
    "TURTLE_SOUP_PLUS_ONE": {
        "lookback"    : {"type": int, "lo": 10, "hi": 30},
        "min_sessions": {"type": int, "lo": 1,  "hi": 7 },
        "sl_pct"      : _SL_PCT_RANGE,
    },
    "EIGHTY_TWENTY": {
        "threshold": {"type": float, "lo": 0.05, "hi": 0.40},
        "sl_pct"   : _SL_PCT_RANGE,
    },
    "MOMENTUM_PINBALL": {
        "rsi_period": {"type": int,   "lo": 2,    "hi": 8   },
        "oversold"  : {"type": float, "lo": 10.0, "hi": 40.0},
        "overbought": {"type": float, "lo": 60.0, "hi": 90.0},
        "sl_pct"    : _SL_PCT_RANGE,
    },
    "THE_ANTI": {
        "k_period": {"type": int, "lo": 3,  "hi": 20},
        "d_period": {"type": int, "lo": 5,  "hi": 30},
        "sl_pct"  : _SL_PCT_RANGE,
    },
    "HOLY_GRAIL": {
        "adx_period"   : {"type": int,   "lo": 8,    "hi": 25  },
        "ema_period"   : {"type": int,   "lo": 10,   "hi": 35  },
        "adx_threshold": {"type": float, "lo": 20.0, "hi": 45.0},
        "sl_pct"       : _SL_PCT_RANGE,
    },
    "ADX_GAPPER": {
        "adx_period"   : {"type": int,   "lo": 7,    "hi": 20  },
        "di_period"    : {"type": int,   "lo": 14,   "hi": 42  },
        "adx_threshold": {"type": float, "lo": 20.0, "hi": 45.0},
        "sl_pct"       : _SL_PCT_RANGE,
    },
    "HV_NR4": {
        "short_period"      : {"type": int,   "lo": 3,    "hi": 12  },
        "long_period"       : {"type": int,   "lo": 40,   "hi": 200 },
        "hv_ratio_threshold": {"type": float, "lo": 0.20, "hi": 0.80},
        "sl_pct"            : _SL_PCT_RANGE,
    },
    "ID_NR4": {
        "require_both": {"type": bool},
        "sl_pct"      : _SL_PCT_RANGE,
    },
    "MACD_DIVERGENCE": {
        "fast_period"  : {"type": int,   "lo": 5,    "hi": 20  },
        "slow_period"  : {"type": int,   "lo": 18,   "hi": 35  },
        "signal_period": {"type": int,   "lo": 5,    "hi": 15  },
        "lookback"     : {"type": int,   "lo": 8,    "hi": 25  },
        "sl_pct"       : _SL_PCT_RANGE,
    },
    "BOLLINGER_SQUEEZE": {
        "period"          : {"type": int,   "lo": 10,  "hi": 30  },
        "num_stddev"      : {"type": float, "lo": 1.2, "hi": 3.0 },
        "squeeze_lookback": {"type": int,   "lo": 8,   "hi": 40  },
        "sl_pct"          : _SL_PCT_RANGE,
    },
    "EMA_TREND_PULLBACK": {
        "fast_ema": {"type": int, "lo": 10,  "hi": 60 },
        "slow_ema": {"type": int, "lo": 80,  "hi": 250},
        "lookback": {"type": int, "lo": 2,   "hi": 12 },
        "sl_pct"  : _SL_PCT_RANGE,
    },
    "N_DOWN_REVERSAL": {
        "n_down"      : {"type": int, "lo": 2,  "hi": 8  },
        "trend_period": {"type": int, "lo": 30, "hi": 250},
        "sl_pct"      : _SL_PCT_RANGE,
    },
    "VOLUME_CLIMAX": {
        "vol_period"     : {"type": int,   "lo": 10,  "hi": 40  },
        "vol_multiplier" : {"type": float, "lo": 1.2, "hi": 3.5 },
        "atr_period"     : {"type": int,   "lo": 7,   "hi": 25  },
        "atr_multiplier" : {"type": float, "lo": 1.0, "hi": 3.5 },
        "close_threshold": {"type": float, "lo": 0.10, "hi": 0.40},
        "sl_pct"         : _SL_PCT_RANGE,
    },
    "TWO_PERIOD_ROC"        : {"sl_pct": _SL_PCT_RANGE},
    "WHIPLASH"              : {"sl_pct": _SL_PCT_RANGE},
    "THREE_DAY_GAP_REVERSAL": {"sl_pct": _SL_PCT_RANGE},
}

# ── Peak-diversity tolerances ──────────────────────────────────────────────────
# Two results are treated as the SAME local maximum if EVERY parameter
# differs by less than its tolerance.  If ANY parameter exceeds its tolerance
# the two results are considered DIFFERENT peaks.
# Tune these so nearby grid points cluster into one peak while
# genuinely different configurations stay separate.

PEAK_TOLERANCES: dict[str, dict[str, float]] = {
    "RSI_EXTREME"          : {"period": 4,    "overbought": 6.0, "oversold": 6.0,   "sl_pct": 0.75},
    "TURTLE_SOUP"          : {"lookback": 4,  "min_sessions": 2,                    "sl_pct": 0.75},
    "TURTLE_SOUP_PLUS_ONE" : {"lookback": 4,  "min_sessions": 2,                    "sl_pct": 0.75},
    "EIGHTY_TWENTY"        : {"threshold": 0.06,                                    "sl_pct": 0.75},
    "MOMENTUM_PINBALL"     : {"rsi_period": 1, "oversold": 6.0, "overbought": 6.0, "sl_pct": 0.75},
    "THE_ANTI"             : {"k_period": 3,  "d_period": 5,                        "sl_pct": 0.75},
    "HOLY_GRAIL"           : {"adx_period": 3, "ema_period": 5, "adx_threshold": 6.0,"sl_pct": 0.75},
    "ADX_GAPPER"           : {"adx_period": 3, "di_period": 7,  "adx_threshold": 6.0,"sl_pct": 0.75},
    "HV_NR4"               : {"short_period": 2, "long_period": 30, "hv_ratio_threshold": 0.12, "sl_pct": 0.75},
    "ID_NR4"               : {"require_both": 0, "sl_pct": 0.75},  # bool → must match exactly
    "MACD_DIVERGENCE"      : {"fast_period": 3,  "slow_period": 4, "signal_period": 2, "lookback": 4, "sl_pct": 0.75},
    "BOLLINGER_SQUEEZE"    : {"period": 4,  "num_stddev": 0.4, "squeeze_lookback": 8, "sl_pct": 0.75},
    "EMA_TREND_PULLBACK"   : {"fast_ema": 10, "slow_ema": 30, "lookback": 2,          "sl_pct": 0.75},
    "N_DOWN_REVERSAL"      : {"n_down": 1,  "trend_period": 40,                       "sl_pct": 0.75},
    "VOLUME_CLIMAX"        : {"vol_period": 5, "vol_multiplier": 0.4, "atr_period": 4, "atr_multiplier": 0.4, "close_threshold": 0.08, "sl_pct": 0.75},
    "TWO_PERIOD_ROC"        : {"sl_pct": 0.75},
    "WHIPLASH"              : {"sl_pct": 0.75},
    "THREE_DAY_GAP_REVERSAL": {"sl_pct": 0.75},
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _grid_combos(grid: dict[str, list]) -> list[dict]:
    """Generate all parameter combinations from a grid dict."""
    keys   = list(grid.keys())
    values = list(grid.values())
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def _is_valid_combo(setup_name: str, params: dict) -> bool:
    """Filter out obviously meaningless parameter combinations."""
    if setup_name in ("RSI_EXTREME", "MOMENTUM_PINBALL"):
        # Overbought must be strictly above oversold with enough separation
        if params.get("overbought", 70) - params.get("oversold", 30) < 25:
            return False
    if setup_name in ("TURTLE_SOUP", "TURTLE_SOUP_PLUS_ONE"):
        # min_sessions must be less than lookback
        if params.get("min_sessions", 4) >= params.get("lookback", 20):
            return False
    if setup_name == "THE_ANTI":
        # %D period must be at least as long as %K period
        if params.get("d_period", 10) <= params.get("k_period", 7):
            return False
    if setup_name == "MACD_DIVERGENCE":
        # Slow EMA must be meaningfully longer than fast EMA
        if params.get("slow_period", 26) <= params.get("fast_period", 12) + 3:
            return False
    if setup_name == "EMA_TREND_PULLBACK":
        # Trend (slow) EMA must be substantially longer than pullback (fast) EMA
        if params.get("slow_ema", 200) < params.get("fast_ema", 50) + 20:
            return False
    return True


def _sample_random_combos(
    setup_name: str,
    n_samples: int,
    seed: int | None = None,
) -> list[dict]:
    """
    Draw `n_samples` random parameter combinations from PARAM_RANGES_RANDOM.
    Invalid combos (per _is_valid_combo) are discarded; sampling continues
    until n_samples valid combos are found or 20× attempts are exhausted.
    """
    ranges = PARAM_RANGES_RANDOM.get(setup_name)
    if not ranges:
        return []

    rng    = _random.Random(seed)
    combos: list[dict] = []
    max_attempts = n_samples * 20

    for _ in range(max_attempts):
        if len(combos) >= n_samples:
            break
        params: dict = {}
        for pname, pdef in ranges.items():
            ptype = pdef["type"]
            if ptype is bool:
                params[pname] = rng.choice([True, False])
            elif ptype is int:
                params[pname] = rng.randint(pdef["lo"], pdef["hi"])
            else:  # float
                params[pname] = round(rng.uniform(pdef["lo"], pdef["hi"]), 2)
        if _is_valid_combo(setup_name, params):
            combos.append(params)

    return combos


def _same_peak(p1: dict, p2: dict, setup_name: str) -> bool:
    """
    Return True if p1 and p2 are in the same local neighbourhood
    (every parameter differs by less than its peak tolerance).
    """
    tols = PEAK_TOLERANCES.get(setup_name, {})
    for key in p1:
        if key not in p2:
            continue
        tol  = tols.get(key, 2 if isinstance(p1[key], int) else 0.05)
        diff = abs(float(p1[key]) - float(p2[key]))
        if diff > tol:
            return False   # at least one param is far apart → different peak
    return True            # all params within tolerance → same peak


def _find_top_peaks(
    scored: list[tuple],
    setup_name: str,
    top_n: int,
) -> list[dict]:
    """
    From a list of (params, combined_score, avg_return, sl_rate) sorted by combined
    score descending, return the top_n DIVERSE peaks.

    Returns a list of dicts: [{"params": ..., "est_avg_return": ..., ...}, ...]
    """
    peaks: list[dict] = []
    for params, combined, avg_ret, sl_rate in sorted(scored, key=lambda x: -x[1]):
        if len(peaks) >= top_n:
            break
        if not any(_same_peak(params, p["params"], setup_name) for p in peaks):
            peaks.append({
                "params"        : params,
                "est_avg_return": round(avg_ret, 4),
                "est_sl_rate"   : round(sl_rate, 4),
                "est_combined"  : round(combined, 4),
            })
    return peaks


def _load_setup_classes() -> dict[str, type]:
    """
    Dynamically load all setup classes from the Trading Setups directory.
    Returns {setup_name: class}.
    """
    classes: dict[str, type] = {}
    for py_file in sorted(_SETUP_DIR.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module_name = f"_hs_setup_{py_file.stem}"
        try:
            spec   = importlib.util.spec_from_file_location(module_name, py_file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)  # type: ignore[attr-defined]
        except Exception as exc:
            log.warning(f"hypersearch: cannot load {py_file.name} — {exc}")
            continue
        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if (inspect.isclass(obj) and issubclass(obj, BaseSetup)
                    and obj is not BaseSetup and getattr(obj, "name", "")):
                classes[obj.name] = obj
    return classes


def _load_stock_data(days: int) -> dict[str, object]:
    stocks    = get_active_stocks()
    stock_dfs = {}
    for s in stocks:
        df = get_ohlcv(s["symbol"], days=days)
        if not df.empty:
            stock_dfs[s["symbol"]] = df
    return stock_dfs


# ── Parallel workers ───────────────────────────────────────────────────────────
# _worker_init and _worker_combo_task are imported from backtester.py.
# They are defined at module level there so ProcessPoolExecutor (spawn mode on
# Windows) can pickle and send them to worker processes.
#
# _worker_combo_task(setup_file, class_name, params, stride) → stats dict
#   Runs the full backtest for one param combination using stock data that was
#   loaded into each worker process by _worker_init at pool startup.
#
# _worker_init(serialized_stock_dfs: bytes)
#   Called once per worker process at pool startup; deserialises stock data
#   into a module-level global so tasks don't carry it over IPC.

def _serialise_stock_dfs(stock_dfs: dict) -> bytes:
    """Pickle stock data once for the pool initialiser."""
    data = pickle.dumps(stock_dfs)
    log.info(
        f"hypersearch: stock data serialised  "
        f"({len(data)/1e6:.1f} MB, {len(stock_dfs)} stocks)"
    )
    return data


# ── Core search ────────────────────────────────────────────────────────────────

def search_one_setup(
    setup_name: str,
    grids: dict[str, dict[str, list]],
) -> list[dict]:
    """Return the filtered list of parameter combos for this setup's grid."""
    grid = grids.get(setup_name)
    if not grid:
        return []
    return [c for c in _grid_combos(grid) if _is_valid_combo(setup_name, c)]


def _validate_results(
    results: dict[str, dict],
    setup_classes: dict[str, type],
    stock_dfs_full: dict,
    stride: int = 2,
    max_workers: int = _CPU_COUNT,
) -> None:
    """Validate every setup's best params at full backtester precision."""
    tasks = {
        name: (inspect.getfile(setup_classes[name]),
               setup_classes[name].__name__,
               res["params"])
        for name, res in results.items()
        if name in setup_classes
    }
    log.info(
        f"hypersearch: [2/2] validation — {len(tasks)} setup(s) "
        f"at stride={stride}, {max_workers} processes"
    )
    serialized_dfs = _serialise_stock_dfs(stock_dfs_full)
    t0_all = time.time()
    with ProcessPoolExecutor(
        max_workers=min(max_workers, len(tasks)),
        initializer=_worker_init,
        initargs=(serialized_dfs,),
    ) as pool:
        futures = {
            pool.submit(_worker_combo_task, setup_file, class_name, params, stride): name
            for name, (setup_file, class_name, params) in tasks.items()
        }
        for fut in as_completed(futures):
            name   = futures[fut]
            vstats = fut.result()
            res    = results[name]
            v_avg  = vstats.get("best_avg_return", 0.0)
            v_sl   = vstats.get("sl_rate", 0.0)
            res["validated_avg_return"] = v_avg
            res["validated_sl_rate"]    = v_sl
            res["validated_combined"]   = _combined_score(v_avg, v_sl)
            res["validated_days"]       = vstats.get("best_days", 1)
            res["validated_n"]          = vstats.get("tested", 0)
            res["validated_by_day"]     = vstats.get("by_day", {})
            log.info(
                f"  [{name}] valid Avg={v_avg:+.2%}  SL={v_sl:.0%}  "
                f"score={res['validated_combined']:+.2%}  "
                f"d={res['validated_days']}  n={res['validated_n']}"
            )
    log.info(f"hypersearch: validation done in {time.time() - t0_all:.0f}s")


def run_search(
    target_setup: str | None = None,
    quick: bool = False,
    stride: int | None = None,
    history_days: int = 700,
    validate: bool = True,
    max_workers: int = _CPU_COUNT,
    force_rerun: bool = False,
) -> dict[str, dict]:
    grids  = _QUICK_GRIDS if quick else PARAM_GRIDS
    stride = stride or (15 if quick else 10)

    # ── Phase 1: parallel grid search ────────────────────────────────────────
    log.info(
        f"hypersearch: [1/2] grid search  "
        f"(stride={stride}, {history_days} days, {max_workers} processes)"
    )
    stock_dfs_search = _load_stock_data(history_days)
    setup_classes    = _load_setup_classes()

    names_to_search = (
        [target_setup] if target_setup else sorted(grids.keys())
    )

    # Skip setups that already have optimal params unless --force-rerun
    if not force_rerun:
        existing = load_optimal_params()
        skipped  = [n for n in names_to_search if n in existing]
        names_to_search = [n for n in names_to_search if n not in existing]
        if skipped:
            log.info(
                f"hypersearch: skipping {len(skipped)} already-tuned setup(s) "
                f"(use --force-rerun to override): {', '.join(sorted(skipped))}"
            )
        if not names_to_search:
            log.info(
                "hypersearch: all target setups already have optimal params — nothing to do"
            )
            return {}

    # Build flat task list: (name, setup_file, class_name, params).
    # All picklable — no setup class objects cross the process boundary.
    all_tasks: list[tuple[str, str, str, dict]] = []
    combo_counts: dict[str, int] = {}
    for name in names_to_search:
        cls = setup_classes.get(name)
        if cls is None:
            log.warning(f"hypersearch: setup '{name}' not found — skipping")
            continue
        setup_file = inspect.getfile(cls)
        class_name = cls.__name__
        combos     = search_one_setup(name, grids)
        combo_counts[name] = len(combos)
        all_tasks.extend((name, setup_file, class_name, p) for p in combos)

    total = len(all_tasks)
    log.info(f"hypersearch: {total} combinations across {len(combo_counts)} setup(s)")

    serialized_dfs = _serialise_stock_dfs(stock_dfs_search)
    scored: dict[str, list[tuple[dict, float, dict]]] = {n: [] for n in combo_counts}
    done    = 0
    t_start = time.time()

    with ProcessPoolExecutor(
        max_workers=min(max_workers, total),
        initializer=_worker_init,
        initargs=(serialized_dfs,),
    ) as pool:
        futures = {
            pool.submit(_worker_combo_task, setup_file, class_name, params, stride):
                (name, params)
            for name, setup_file, class_name, params in all_tasks
        }
        for fut in as_completed(futures):
            name, params = futures[fut]
            stats   = fut.result()
            avg_ret = stats.get("best_avg_return", 0.0)
            sl_rate = stats.get("sl_rate", 0.0)
            score   = _combined_score(avg_ret, sl_rate)
            n       = stats.get("tested", 0)
            done   += 1
            if n >= MIN_OBS:
                scored[name].append((params, score, stats))
            if done % max(1, total // 10) == 0 or done == total:
                log.info(f"  {done}/{total} combos done  ({time.time()-t_start:.0f}s)")

    log.info(f"hypersearch: search pass done in {time.time() - t_start:.0f}s")

    results: dict[str, dict] = {}
    for name in combo_counts:
        candidates = scored.get(name, [])
        if not candidates:
            log.warning(f"  [{name}] no combo reached {MIN_OBS} observations")
            continue
        best_params, best_score, best_stats = max(
            candidates, key=lambda x: (x[1], x[2].get("tested", 0))
        )
        results[name] = {
            "params"            : best_params,
            "est_avg_return"    : round(best_stats.get("best_avg_return", 0), 4),
            "est_sl_rate"       : round(best_stats.get("sl_rate", 0), 4),
            "est_combined"      : round(best_score, 4),
            "est_days"          : best_stats.get("best_days", 1),
            "est_n"             : best_stats.get("tested", 0),
            "combinations_tried": combo_counts[name],
        }
        log.info(
            f"  [{name}] selected: {best_params}  "
            f"est Avg={best_stats.get('best_avg_return',0):+.2%}  "
            f"SL={best_stats.get('sl_rate',0):.0%}  "
            f"score={best_score:+.2%}  d={best_stats.get('best_days')}  "
            f"n={best_stats.get('tested', 0)}"
        )

    if validate and results:
        log.info(
            f"hypersearch: loading full data for validation "
            f"({BACKTEST_WINDOW_DAYS} days)..."
        )
        stock_dfs_full = _load_stock_data(BACKTEST_WINDOW_DAYS)
        _validate_results(results, setup_classes, stock_dfs_full,
                          stride=2, max_workers=max_workers)

    log.info(f"hypersearch: total time {time.time() - t_start:.0f}s")
    return results


def random_search_one_setup(
    setup_name: str,
    n_samples: int,
    seed: int | None = None,
) -> list[dict]:
    """Return n_samples valid random parameter combos for this setup."""
    if setup_name not in PARAM_RANGES_RANDOM:
        return []
    return _sample_random_combos(setup_name, n_samples, seed=seed)


def _validate_peaks(
    peaks_by_setup: dict[str, list[dict]],
    setup_classes: dict[str, type],
    stock_dfs_full: dict,
    stride: int = 2,
    max_workers: int = _CPU_COUNT,
) -> None:
    """Validate every peak of every setup concurrently via ProcessPoolExecutor."""
    flat_tasks: list[tuple[str, int, str, str, dict]] = []
    for name, peaks in peaks_by_setup.items():
        cls = setup_classes.get(name)
        if cls is None:
            continue
        setup_file = inspect.getfile(cls)
        class_name = cls.__name__
        for rank, peak in enumerate(peaks):
            flat_tasks.append((name, rank, setup_file, class_name, peak["params"]))

    total_peaks = len(flat_tasks)
    log.info(
        f"hypersearch: [2/2] validating {total_peaks} peak(s) "
        f"at stride={stride}, {max_workers} processes"
    )
    serialized_dfs = _serialise_stock_dfs(stock_dfs_full)
    t0_all = time.time()

    with ProcessPoolExecutor(
        max_workers=min(max_workers, total_peaks),
        initializer=_worker_init,
        initargs=(serialized_dfs,),
    ) as pool:
        futures = {
            pool.submit(_worker_combo_task, setup_file, class_name, params, stride):
                (name, rank)
            for name, rank, setup_file, class_name, params in flat_tasks
        }
        for fut in as_completed(futures):
            name, rank = futures[fut]
            vstats = fut.result()
            peak   = peaks_by_setup[name][rank]
            v_avg  = vstats.get("best_avg_return", 0.0)
            v_sl   = vstats.get("sl_rate", 0.0)
            peak["validated_avg_return"] = v_avg
            peak["validated_sl_rate"]    = v_sl
            peak["validated_combined"]   = _combined_score(v_avg, v_sl)
            peak["validated_days"]       = vstats.get("best_days", 1)
            peak["validated_n"]          = vstats.get("tested", 0)
            peak["validated_by_day"]     = vstats.get("by_day", {})
            log.info(
                f"  [{name}] peak {rank+1}: "
                f"valid Avg={v_avg:+.2%}  SL={v_sl:.0%}  "
                f"score={peak['validated_combined']:+.2%}  "
                f"d={peak['validated_days']}  n={peak['validated_n']}"
            )

    for peaks in peaks_by_setup.values():
        peaks.sort(key=lambda p: -p.get("validated_combined", p.get("est_combined", 0)))
    log.info(f"hypersearch: validation done in {time.time() - t0_all:.0f}s")


def run_random_search(
    target_setup: str | None = None,
    n_samples: int = 100,
    top_n_peaks: int = 3,
    validate: bool = True,
    seed: int | None = 42,
    max_workers: int = _CPU_COUNT,
    force_rerun: bool = False,
) -> dict[str, list[dict]]:
    """
    Random hyperparameter search.

    Returns {setup_name: [peak1, peak2, ...]} sorted by validated (or est) WR.
    All combinations across all setups are evaluated concurrently.
    """
    SEARCH_STRIDE = 12

    log.info(
        f"hypersearch: [1/2] random search  "
        f"(samples={n_samples}/setup, stride={SEARCH_STRIDE}, "
        f"700 days, {max_workers} workers)"
    )
    stock_dfs_search = _load_stock_data(700)
    setup_classes    = _load_setup_classes()

    names = (
        [target_setup] if target_setup
        else sorted(PARAM_RANGES_RANDOM.keys())
    )

    # Skip setups that already have optimal params unless --force-rerun
    if not force_rerun:
        existing = load_optimal_params()
        skipped  = [n for n in names if n in existing]
        names    = [n for n in names if n not in existing]
        if skipped:
            log.info(
                f"hypersearch: skipping {len(skipped)} already-tuned setup(s) "
                f"(use --force-rerun to override): {', '.join(sorted(skipped))}"
            )
        if not names:
            log.info(
                "hypersearch: all target setups already have optimal params — nothing to do"
            )
            return {}

    # Build flat task list across all setups
    all_tasks: list[tuple[str, type, dict]] = []
    setup_combos_map: dict[str, list[dict]] = {}   # keep ordered combo lists per setup
    for name in names:
        cls = setup_classes.get(name)
        if cls is None:
            log.warning(f"hypersearch: setup '{name}' not found — skipping")
            continue
        combos = random_search_one_setup(name, n_samples, seed=seed)
        if not combos:
            log.warning(f"  [{name}] no valid random combos generated")
            continue
        setup_combos_map[name] = combos
        setup_file = inspect.getfile(cls)
        class_name = cls.__name__
        all_tasks.extend((name, setup_file, class_name, p) for p in combos)

    total = len(all_tasks)
    log.info(f"hypersearch: {total} combinations across {len(setup_combos_map)} setup(s)")

    serialized_dfs = _serialise_stock_dfs(stock_dfs_search)
    scored: dict[str, list[tuple[dict, float]]] = {n: [] for n in setup_combos_map}
    done    = 0
    t_start = time.time()

    with ProcessPoolExecutor(
        max_workers=min(max_workers, total),
        initializer=_worker_init,
        initargs=(serialized_dfs,),
    ) as pool:
        futures = {
            pool.submit(_worker_combo_task, setup_file, class_name, params, SEARCH_STRIDE):
                (name, params)
            for name, setup_file, class_name, params in all_tasks
        }
        for fut in as_completed(futures):
            name, params = futures[fut]
            stats   = fut.result()
            avg_ret = stats.get("best_avg_return", 0.0)
            sl_rate = stats.get("sl_rate", 0.0)
            score   = _combined_score(avg_ret, sl_rate)
            n       = stats.get("tested", 0)
            done   += 1
            if n >= MIN_OBS:
                scored[name].append((params, score, avg_ret, sl_rate))
            if done % max(1, total // 10) == 0 or done == total:
                log.info(f"  {done}/{total} combos done  ({time.time()-t_start:.0f}s)")

    log.info(f"hypersearch: search pass done in {time.time() - t_start:.0f}s")

    # Extract top-N peaks per setup
    peaks_by_setup: dict[str, list[dict]] = {}
    for name in setup_combos_map:
        candidates = scored.get(name, [])
        if not candidates:
            log.warning(f"  [{name}] no combo reached {MIN_OBS} observations")
            continue
        peaks = _find_top_peaks(candidates, name, top_n_peaks)
        peaks_by_setup[name] = peaks
        log.info(
            f"  [{name}] {len(peaks)} peak(s) found  "
            f"best est Avg={peaks[0]['est_avg_return']:+.2%}  "
            f"SL={peaks[0]['est_sl_rate']:.0%}  "
            f"score={peaks[0]['est_combined']:+.2%}  params={peaks[0]['params']}"
        )

    # Validate all peaks concurrently
    if validate and peaks_by_setup:
        log.info(
            f"hypersearch: loading full data for validation "
            f"({BACKTEST_WINDOW_DAYS} days)..."
        )
        stock_dfs_full = _load_stock_data(BACKTEST_WINDOW_DAYS)
        _validate_peaks(peaks_by_setup, setup_classes, stock_dfs_full,
                        stride=2, max_workers=max_workers)

    log.info(f"hypersearch: total time {time.time() - t_start:.0f}s")
    return peaks_by_setup


def save_optimal_params(results: dict[str, dict]) -> None:
    from datetime import datetime

    # Load existing entries so skipped setups are not erased
    existing_setups: dict = {}
    if OPTIMAL_PARAMS_PATH.exists():
        try:
            existing_setups = json.loads(OPTIMAL_PARAMS_PATH.read_text()).get("setups", {})
        except Exception:
            pass

    new_entries = {
        name: {
            "params"            : r["params"],
            # authoritative numbers: validated at same precision as backtester
            "best_avg_return"   : r.get("validated_avg_return", r.get("est_avg_return",  0)),
            "best_sl_rate"      : r.get("validated_sl_rate",    r.get("est_sl_rate",     0)),
            "best_combined"     : r.get("validated_combined",   r.get("est_combined",    0)),
            "best_days"         : r.get("validated_days",       r.get("est_days",        1)),
            "tested"            : r.get("validated_n",          r.get("est_n",           0)),
            "by_day"            : r.get("validated_by_day",     r.get("by_day",          {})),
            # search metadata
            "combinations_tried": r["combinations_tried"],
            "est_avg_return"    : r.get("est_avg_return"),
            "est_sl_rate"       : r.get("est_sl_rate"),
            "est_combined"      : r.get("est_combined"),
        }
        for name, r in results.items()
    }

    # Merge: new entries override existing for re-run setups; rest preserved
    merged = {**existing_setups, **new_entries}

    payload = {
        "generated_at": datetime.now().isoformat(),
        "note": (
            "Generated by hyperparameter_search.py. "
            "best_win_rate is validated at stride=2 / full history — "
            "matches backtester.py output. "
            "setup_loader.py reads 'params' for each setup at startup."
        ),
        "setups": merged,
    }
    OPTIMAL_PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    OPTIMAL_PARAMS_PATH.write_text(json.dumps(payload, indent=2))
    log.info(
        f"hypersearch: optimal params written to {OPTIMAL_PARAMS_PATH}  "
        f"({len(new_entries)} updated, {len(merged) - len(new_entries)} preserved)"
    )


def load_optimal_params() -> dict[str, dict]:
    """Return {setup_name: params_dict}. Empty dict if file missing."""
    if not OPTIMAL_PARAMS_PATH.exists():
        return {}
    try:
        data = json.loads(OPTIMAL_PARAMS_PATH.read_text())
        return {
            name: info["params"]
            for name, info in data.get("setups", {}).items()
        }
    except Exception:
        return {}


# ── CLI ────────────────────────────────────────────────────────────────────────

def _parse_arg(flag: str, default):
    """Return the value after `flag` in sys.argv, cast to type(default)."""
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            try:
                return type(default)(sys.argv[i + 1])
            except (ValueError, TypeError):
                pass
    return default


if __name__ == "__main__":
    use_random   = "--random"      in sys.argv
    quick        = "--quick"       in sys.argv
    no_validate  = "--no-validate" in sys.argv
    force_rerun  = "--force-rerun" in sys.argv
    n_samples    = _parse_arg("--samples", 100)
    top_n_peaks  = _parse_arg("--peaks",   3)
    max_workers  = _parse_arg("--workers", _CPU_COUNT)

    target_setup = None
    for i, arg in enumerate(sys.argv):
        if arg == "--setup" and i + 1 < len(sys.argv):
            target_setup = sys.argv[i + 1]
            break

    existing_count = len(load_optimal_params())

    print("\n" + "=" * 80)
    print("  Signal Infomer — Hyperparameter Search")
    if use_random:
        print(f"  Mode:       RANDOM  ({n_samples} samples/setup, top {top_n_peaks} peaks)")
    else:
        print(f"  Mode:       GRID  ({'quick' if quick else 'full'})")
    print(f"  Workers:    {max_workers} processes  (CPU count={_CPU_COUNT})")
    print(f"  Validation: {'SKIPPED (--no-validate)' if no_validate else 'stride=2 + full data (matches backtester.py)'}")
    if target_setup:
        print(f"  Setup:      {target_setup}")
    if force_rerun:
        print(f"  Re-run:     ALL  (--force-rerun)")
    elif existing_count:
        print(f"  Skipping:   {existing_count} already-tuned setup(s)  (--force-rerun to override)")
    print("=" * 80 + "\n")

    # ── Run search ──────────────────────────────────────────────────────────
    if use_random:
        peaks_by_setup = run_random_search(
            target_setup=target_setup,
            n_samples=n_samples,
            top_n_peaks=top_n_peaks,
            validate=not no_validate,
            max_workers=max_workers,
            force_rerun=force_rerun,
        )

        if not peaks_by_setup:
            if not force_rerun and load_optimal_params():
                print("All target setups already tuned — nothing to do.")
                print("Use --force-rerun to redo all, or --setup NAME to target one setup.")
            else:
                print("No results — ensure DB has data (run initialize.py first).")
                sys.exit(1)
            sys.exit(0)

        # ── Print multi-peak table ──────────────────────────────────────────
        has_validated = any(
            "validated_combined" in p
            for peaks in peaks_by_setup.values()
            for p in peaks
        )
        sort_col = "validated_combined" if has_validated else "est_combined"

        print(f"\n{'Setup / Peak':<36}  {'Est Score':>9}  {'V.Score':>7}  {'V.Avg':>7}  {'SL%':>5}  {'Day':>4}  {'n':>6}  Params")
        print("-" * 115)

        for name in sorted(peaks_by_setup, key=lambda n: -peaks_by_setup[n][0].get(sort_col, 0)):
            peaks = peaks_by_setup[name]
            for rank, p in enumerate(peaks, 1):
                label  = f"{name}  [peak {rank}{'  *' if rank == 1 else ''}]"
                pstr   = "  ".join(f"{k}={v}" for k, v in p["params"].items())
                v_comb = p.get("validated_combined")
                v_avg  = p.get("validated_avg_return")
                v_sl   = p.get("validated_sl_rate")
                vday   = p.get("validated_days", "?")
                vn     = p.get("validated_n", "?")
                print(
                    f"  {label:<34}  "
                    f"{p['est_combined']:>+8.2%}  "
                    f"{(f'{v_comb:+.2%}' if v_comb is not None else '—'):>7}  "
                    f"{(f'{v_avg:+.2%}' if v_avg  is not None else '—'):>7}  "
                    f"{(f'{v_sl:.0%}'   if v_sl   is not None else '—'):>5}  "
                    f"{'d'+str(vday):>4}  {str(vn):>6}  {pstr}"
                )
            print()

        if has_validated:
            print("  Est Score = combined_score(avg_return, sl_rate) fast estimate (stride=12, 700d)")
            print("  V.Score   = validated combined score (stride=2, full data)")
            print("  V.Avg     = validated avg return per trade")
            print("  SL%       = validated stoploss hit rate")
            print("  [peak 1 *] is saved as the selected params for each setup")
        else:
            print("  NOTE: --no-validate used. Validated scores not computed.")

        # Save best peak per setup (peak 1 after re-sort by validated WR)
        grid_style_results = {}
        for name, peaks in peaks_by_setup.items():
            best = peaks[0]
            grid_style_results[name] = {
                "params"              : best["params"],
                "est_avg_return"      : best.get("est_avg_return", 0),
                "est_sl_rate"         : best.get("est_sl_rate", 0),
                "est_combined"        : best.get("est_combined", 0),
                "validated_avg_return": best.get("validated_avg_return"),
                "validated_sl_rate"   : best.get("validated_sl_rate"),
                "validated_combined"  : best.get("validated_combined"),
                "validated_days"      : best.get("validated_days"),
                "validated_n"         : best.get("validated_n"),
                "validated_by_day"    : best.get("validated_by_day", {}),
                "combinations_tried"  : n_samples,
                "all_peaks"           : [
                    {
                        "params"              : p["params"],
                        "est_avg_return"      : p.get("est_avg_return"),
                        "est_sl_rate"         : p.get("est_sl_rate"),
                        "est_combined"        : p.get("est_combined"),
                        "validated_avg_return": p.get("validated_avg_return"),
                        "validated_sl_rate"   : p.get("validated_sl_rate"),
                        "validated_combined"  : p.get("validated_combined"),
                        "validated_days"      : p.get("validated_days"),
                        "validated_n"         : p.get("validated_n"),
                    }
                    for p in peaks
                ],
            }
        save_optimal_params(grid_style_results)

    else:
        # ── Original grid search ────────────────────────────────────────────
        results = run_search(
            target_setup=target_setup,
            quick=quick,
            validate=not no_validate,
            max_workers=max_workers,
            force_rerun=force_rerun,
        )

        if not results:
            if not force_rerun and load_optimal_params():
                print("All target setups already tuned — nothing to do.")
                print("Use --force-rerun to redo all, or --setup NAME to target one setup.")
            else:
                print("No results — ensure DB has data (run initialize.py first).")
                sys.exit(1)
            sys.exit(0)

        has_validated = any("validated_avg_return" in r for r in results.values())

        if has_validated:
            hdr = f"{'Setup':<30}  {'E.Score':>7}  {'V.Score':>7}  {'V.Avg':>7}  {'SL%':>5}  {'Day':>4}  {'n':>6}  {'Combos':>7}  Best params"
            print("\n" + hdr)
            print("-" * len(hdr))
            for name, r in sorted(results.items(),
                                   key=lambda x: -x[1].get("validated_combined",
                                                            x[1].get("est_combined", 0))):
                params_str = "  ".join(f"{k}={v}" for k, v in r["params"].items())
                print(
                    f"{name:<30}  {r.get('est_combined', 0):>+6.2%}  "
                    f"{r.get('validated_combined', 0):>+6.2%}  "
                    f"{r.get('validated_avg_return', 0):>+6.2%}  "
                    f"{r.get('validated_sl_rate', 0):>4.0%}  "
                    f"d{r.get('validated_days', r.get('est_days', 1)):>3}  "
                    f"{r.get('validated_n', r.get('est_n', 0)):>6}  "
                    f"{r['combinations_tried']:>7}  {params_str}"
                )
            print("\n  E.Score = combined_score(avg_ret, sl_rate) fast estimate (stride=10, 700d)")
            print("  V.Score = validated combined score (stride=2, full data)")
            print("  V.Avg   = validated avg return per trade | SL% = stoploss hit rate")
        else:
            hdr = f"{'Setup':<30}  {'Est Score':>9}  {'Est Avg':>8}  {'Est SL%':>7}  {'Day':>4}  {'n':>6}  {'Combos':>7}  Best params"
            print("\n" + hdr)
            print("-" * len(hdr))
            for name, r in sorted(results.items(), key=lambda x: -x[1].get("est_combined", 0)):
                params_str = "  ".join(f"{k}={v}" for k, v in r["params"].items())
                print(
                    f"{name:<30}  {r.get('est_combined', 0):>+8.2%}  "
                    f"{r.get('est_avg_return', 0):>+7.2%}  "
                    f"{r.get('est_sl_rate', 0):>6.0%}  "
                    f"d{r.get('est_days', 1):>3}  {r.get('est_n', 0):>6}  "
                    f"{r['combinations_tried']:>7}  {params_str}"
                )
            print("\n  NOTE: --no-validate used. Run backtester.py for accurate scores.")

        save_optimal_params(results)

    print(f"\nSaved to {OPTIMAL_PARAMS_PATH}\n")
