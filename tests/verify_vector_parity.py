"""
Parity check: for every legacy setup that now has vector_signals(), verify the
vectorised signals match the per-bar signal() path bar-for-bar (same fired
bars, same direction) on real stored OHLCV data.

Usage:  python tests/verify_vector_parity.py [n_symbols]
"""
import sys
import warnings
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
warnings.filterwarnings("ignore")

from data.db import get_active_stocks, get_ohlcv
from notifications.whatsapp import _direction_of
from setup_loader import load_setups

N_SYMBOLS = int(sys.argv[1]) if len(sys.argv) > 1 else 8
DAYS      = 500

# Setups whose signal() is the auto-derived VectorSetup shim are tautologically
# consistent; parity only matters for legacy classes with hand-written signal().
from core.vector_setup import VectorSetup


def main() -> int:
    stocks  = get_active_stocks()[:N_SYMBOLS]
    setups  = [s for s in load_setups(use_optimal_params=False)
               if hasattr(s, "vector_signals") and not isinstance(s, VectorSetup)]
    print(f"Checking {len(setups)} legacy setups on {len(stocks)} symbols...")

    failures = 0
    for setup in setups:
        mism_total = 0
        fired_total = 0
        for st in stocks:
            df = get_ohlcv(st["symbol"], days=DAYS)
            if len(df) < setup.min_periods + 5:
                continue
            dirs = np.asarray(setup.vector_signals(df), dtype=float)
            n = len(df)
            for i in range(setup.min_periods, n):
                sub = df.iloc[:i + 1]
                try:
                    res = setup.signal(sub, st["symbol"])
                except Exception:
                    res = None
                slow_fired = bool(res and res.signal)
                slow_dir = 0
                if slow_fired:
                    d = _direction_of(res.to_dict())
                    slow_dir = -1 if d == "sell" else 1
                vec_dir = int(dirs[i]) if not np.isnan(dirs[i]) else 0
                if slow_fired:
                    fired_total += 1
                if (vec_dir != 0) != slow_fired or (slow_fired and vec_dir != slow_dir):
                    mism_total += 1
                    if mism_total <= 3:
                        print(f"  MISMATCH {setup.name} {st['symbol']} "
                              f"{df.index[i].date()}: slow={slow_dir if slow_fired else 0} "
                              f"vec={vec_dir}")
        status = "OK " if mism_total == 0 else "FAIL"
        print(f"  [{status}] {setup.name:<24} fired={fired_total:>4}  mismatches={mism_total}")
        if mism_total:
            failures += 1
    print(f"\n{len(setups) - failures}/{len(setups)} legacy setups match exactly")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
