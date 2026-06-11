"""
Turtle Soup (Connors & Raschke, Chapter 4)

Setup: The market makes a new 20-day high or low, but the PREVIOUS 20-day
extreme occurred at least 4 trading sessions earlier — signalling a likely
false breakout / reversal.

Signal=True when (buy):
  - Latest bar made a new 20-day low
  - The prior 20-day low was set >= 4 sessions ago

Metadata:
  signal_type    : "buy" or "sell"
  new_extreme    : new 20-day low or high value
  prev_extreme   : level of the previous 20-day extreme
  days_since_prev: sessions between previous extreme and today
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from core.base_setup import BaseSetup, SignalResult


class TurtleSoupSetup(BaseSetup):
    name        = "TURTLE_SOUP"
    description = "New 20-day extreme with prior extreme >= 4 sessions ago (false-breakout reversal)"
    min_periods = 25

    def __init__(self, lookback: int = 20, min_sessions: int = 4):
        self.lookback      = lookback
        self.min_sessions  = min_sessions

    # ── helpers ──────────────────────────────────────────────────────────────

    def _check_side(self, df: pd.DataFrame, side: str) -> dict | None:
        """
        Returns metadata dict if the signal fires, else None.
        side = "buy"  → look at lows
        side = "sell" → look at highs
        """
        col = "low" if side == "buy" else "high"
        series = df[col]

        today_val = series.iloc[-1]
        window    = series.iloc[-(self.lookback + 1):-1]   # prev 20 bars excl today

        # Strict inequality: a retest of the exact prior extreme is NOT a new
        # breakout and should not fire — only a genuine new extreme qualifies.
        if side == "buy":
            is_new_extreme = today_val < window.min()
        else:
            is_new_extreme = today_val > window.max()

        if not is_new_extreme:
            return None

        # Date when the prior extreme was set (within the 20-bar window)
        if side == "buy":
            prev_extreme_loc = window.idxmin()
        else:
            prev_extreme_loc = window.idxmax()

        prev_extreme_val = series.loc[prev_extreme_loc]
        all_idx          = list(df.index)
        today_pos        = len(df) - 1
        prev_pos         = all_idx.index(prev_extreme_loc)
        days_since       = today_pos - prev_pos

        if days_since < self.min_sessions:
            return None

        return {
            "signal_type"    : side,
            "new_extreme"    : round(float(today_val), 2),
            "prev_extreme"   : round(float(prev_extreme_val), 2),
            "days_since_prev": days_since,
            "lookback"       : self.lookback,
        }

    # ── public ───────────────────────────────────────────────────────────────

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        valid, err = self.validate_data(df)
        if not valid:
            return self._error_result(symbol, err)

        date = df.index[-1].strftime("%Y-%m-%d")

        meta = self._check_side(df, "buy") or self._check_side(df, "sell")
        if meta:
            return SignalResult(signal=True, symbol=symbol,
                                setup_name=self.name, date=date, metadata=meta)

        return SignalResult(signal=False, symbol=symbol,
                            setup_name=self.name, date=date,
                            metadata={"reason": "no qualifying 20-day extreme"})

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        """Vectorised equivalent. days_since_prev = lookback − argmin/argmax
        offset within the prior window (argmin = first occurrence, matching
        idxmin in the per-bar path). Buy side wins when both fire (matches the
        `or` short-circuit in signal())."""
        import numpy as np
        lb = self.lookback
        low, high = df["low"], df["high"]

        prior_min = low.shift(1).rolling(lb, min_periods=lb).min()
        prior_max = high.shift(1).rolling(lb, min_periods=lb).max()
        new_low   = low  < prior_min
        new_high  = high > prior_max

        # offset of the prior extreme inside the shifted window (0 = oldest bar)
        argmin_off = low.shift(1).rolling(lb, min_periods=lb).apply(np.argmin, raw=True)
        argmax_off = high.shift(1).rolling(lb, min_periods=lb).apply(np.argmax, raw=True)
        days_since_low  = lb - argmin_off
        days_since_high = lb - argmax_off

        long_  = new_low  & (days_since_low  >= self.min_sessions)
        short_ = new_high & (days_since_high >= self.min_sessions) & ~long_
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)
