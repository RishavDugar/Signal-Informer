"""
Turtle Soup Plus One (Connors & Raschke, Chapter 5)

The same setup as Turtle Soup, but the signal fires ONE DAY LATER — after
the market has closed near or below (above) the new extreme, giving late
breakout players one more session to enter before the reversal.

Day-1 conditions (buy):
  - Made a new 20-day low
  - Previous 20-day low >= 3 sessions earlier
  - Day-1 CLOSE is at or below the previous 20-day low

Signal (Day 2, i.e., today = day after the setup):
  - Yesterday was a valid Day-1 setup
  - Entry: buy stop at the earlier 20-day low (watch for today's open/close
    above that level)

Metadata:
  signal_type      : "buy" or "sell"
  setup_day        : date of the Day-1 bar
  entry_level      : previous 20-day extreme (buy stop level)
  days_since_prior : gap between prior extreme and the setup day
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from core.base_setup import BaseSetup, SignalResult


class TurtleSoupPlusOneSetup(BaseSetup):
    name        = "TURTLE_SOUP_PLUS_ONE"
    description = "One day after new 20-day extreme; close at/beyond prior extreme (late-breakout trap)"
    min_periods = 26

    def __init__(self, lookback: int = 20, min_sessions: int = 3):
        self.lookback     = lookback
        self.min_sessions = min_sessions

    def _check_side(self, df: pd.DataFrame, side: str) -> dict | None:
        col   = "low" if side == "buy" else "high"
        series = df[col]
        closes = df["close"]

        # ── Day-1 is yesterday (iloc[-2]) ──────────────────────────────────
        day1_val   = series.iloc[-2]
        day1_close = closes.iloc[-2]
        window     = series.iloc[-(self.lookback + 2):-2]  # 20 bars before day-1

        if side == "buy":
            is_new_extreme = day1_val <= window.min()
        else:
            is_new_extreme = day1_val >= window.max()

        if not is_new_extreme:
            return None

        # Prior extreme within that window
        if side == "buy":
            prior_loc = window.idxmin()
            prior_val = window.min()
            # Day-1 close must be at or below the prior 20-day low
            if day1_close > prior_val:
                return None
        else:
            prior_loc = window.idxmax()
            prior_val = window.max()
            if day1_close < prior_val:
                return None

        all_idx   = list(df.index)
        day1_pos  = len(df) - 2
        prior_pos = all_idx.index(prior_loc)
        days_gap  = day1_pos - prior_pos

        if days_gap < self.min_sessions:
            return None

        return {
            "signal_type"      : side,
            "setup_day"        : df.index[-2].strftime("%Y-%m-%d"),
            "entry_level"      : round(float(prior_val), 2),
            "days_since_prior" : days_gap,
        }

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
                            metadata={"reason": "no day-2 setup"})
