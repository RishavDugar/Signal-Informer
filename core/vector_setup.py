"""
VectorSetup — base class for vectorised trading setups.

Subclasses implement vector_signals(df) which computes the signal for EVERY
bar in one pass (pandas/numpy), returning a Series aligned to df.index with
values:
    +1  → long  signal on that bar (multi-day hold in the daily engine)
    -1  → short signal on that bar (intraday-only in the daily engine)
     0  → no signal

Why: the legacy per-bar signal() protocol re-computes all indicators on a
growing window for every bar — O(n²) per stock. That is unusable on intraday
data (a year of 1-minute bars is ~90k rows) and slow even on daily data once
the setup count grows. vector_signals() computes once per (setup, stock) and
the backtester only iterates the fired bars.

The classic signal() interface is derived automatically from the last bar of
vector_signals(), so VectorSetup subclasses plug into the daily pipeline,
WhatsApp alerts and the signal DB without extra code.

Rules for implementations (no look-ahead):
  • A bar's signal may only use data up to and including that bar.
  • Use rolling()/shift() — never centred windows or future indexing.
  • Fire on the FIRST bar a condition becomes true (cross semantics), not on
    every bar it remains true, unless the setup genuinely re-arms daily.

Optional override:
  vector_stops(df) -> Series of stop-loss PRICES aligned to df.index
  (NaN = no native stop for that bar). Default: the backtester falls back to
  the signal bar's low (long) / high (short), matching BaseSetup.get_stoploss.

Optional class attributes used by hyperparameter_search.py:
  param_grid : {param_name: [values]} — grid-search space (sl_pct added
               automatically by the search if absent).
"""

from __future__ import annotations

from abc import abstractmethod

import numpy as np
import pandas as pd

from core.base_setup import BaseSetup, SignalResult


class VectorSetup(BaseSetup):

    #: optional grid-search space, merged by hyperparameter_search.py
    param_grid: dict[str, list] | None = None

    # ── Vector interface ──────────────────────────────────────────────────────

    @abstractmethod
    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        """Return +1/-1/0 per bar. Must not look ahead."""

    def vector_stops(self, df: pd.DataFrame) -> pd.Series | None:
        """Native stop prices per bar (NaN = none). None = use default."""
        return None

    # ── Derived classic interface ─────────────────────────────────────────────

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        valid, err = self.validate_data(df)
        if not valid:
            return self._error_result(symbol, err)
        try:
            dirs = self.vector_signals(df)
        except Exception as exc:
            return self._error_result(symbol, f"vector_signals failed: {exc}")
        if len(dirs) != len(df):
            return self._error_result(symbol, "vector_signals length mismatch")

        d_last = dirs.iloc[-1] if isinstance(dirs, pd.Series) else dirs[-1]
        d_last = 0 if (d_last is None or (isinstance(d_last, float) and np.isnan(d_last))) else int(d_last)
        fired  = d_last != 0

        meta: dict = {"signal_type": "buy" if d_last > 0 else ("sell" if d_last < 0 else "none")}
        if fired:
            try:
                extra = self.last_bar_metadata(df)
                if extra:
                    meta.update(extra)
            except Exception:
                pass

        idx_last = df.index[-1]
        date_str = idx_last.strftime("%Y-%m-%d") if hasattr(idx_last, "strftime") else str(idx_last)
        return SignalResult(
            signal=fired,
            symbol=symbol,
            setup_name=self.name,
            date=date_str,
            metadata=meta,
        )

    def last_bar_metadata(self, df: pd.DataFrame) -> dict:
        """Optional extra metadata for live alerts. Override where useful."""
        return {}

    def get_stoploss(self, result: SignalResult, df: pd.DataFrame) -> float | None:
        """Use vector_stops for the last bar when provided, else base default."""
        stops = self.vector_stops(df)
        if stops is not None:
            val = stops.iloc[-1] if isinstance(stops, pd.Series) else stops[-1]
            if val is not None and not (isinstance(val, float) and np.isnan(val)):
                return float(val)
            return None
        return super().get_stoploss(result, df)
