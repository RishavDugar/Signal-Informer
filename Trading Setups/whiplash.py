"""
Whiplash (Connors & Raschke, Chapter 12)

A gap-reversal climax pattern.  The market gaps beyond the prior day's
extreme, reverses, and closes in the opposing half of today's range — a
classic exhaustion day.

Buy conditions (all on today's bar):
  1. Today's OPEN < yesterday's LOW  (gap down)
  2. Today's CLOSE > today's OPEN    (closed up from open)
  3. Today's CLOSE is in the TOP 50 % of today's range
     (close > (today_low + today_high) / 2)

Sell conditions (reversed):
  1. Today's OPEN > yesterday's HIGH  (gap up)
  2. Today's CLOSE < today's OPEN
  3. Today's CLOSE in the BOTTOM 50 % of today's range

Entry: buy/sell MOC (today's close — already done when signal fires).
Exit rule: if next day opens AGAINST the position, exit immediately.

Metadata:
  signal_type   : "buy" or "sell"
  gap_pct       : gap size as fraction of yesterday's range
  close_pct     : close position within today's range (0=low, 1=high)
  gap_open      : today's open
  prev_extreme  : yesterday's low (buy) or high (sell)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from core.base_setup import BaseSetup, SignalResult


class WhiplashSetup(BaseSetup):
    name        = "WHIPLASH"
    description = "Gap beyond prior extreme + close reverses into top/bottom half of range"
    min_periods = 2

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        valid, err = self.validate_data(df)
        if not valid:
            return self._error_result(symbol, err)

        date    = df.index[-1].strftime("%Y-%m-%d")
        today   = df.iloc[-1]
        prev    = df.iloc[-2]

        today_range = today["high"] - today["low"]
        prev_range  = prev["high"]  - prev["low"]

        if today_range == 0:
            return SignalResult(signal=False, symbol=symbol,
                                setup_name=self.name, date=date,
                                metadata={"reason": "zero range today"})

        midpoint  = (today["high"] + today["low"]) / 2
        close_pct = (today["close"] - today["low"]) / today_range

        # Buy: gap down + bullish reversal close
        if (today["open"] < prev["low"]
                and today["close"] > today["open"]
                and today["close"] > midpoint):
            gap_pct = (prev["low"] - today["open"]) / max(prev_range, 0.01)
            return SignalResult(
                signal=True, symbol=symbol, setup_name=self.name, date=date,
                metadata={
                    "signal_type" : "buy",
                    "gap_pct"     : round(gap_pct, 3),
                    "close_pct"   : round(close_pct, 3),
                    "gap_open"    : round(float(today["open"]), 2),
                    "prev_extreme": round(float(prev["low"]), 2),
                },
            )

        # Sell: gap up + bearish reversal close
        if (today["open"] > prev["high"]
                and today["close"] < today["open"]
                and today["close"] < midpoint):
            gap_pct = (today["open"] - prev["high"]) / max(prev_range, 0.01)
            return SignalResult(
                signal=True, symbol=symbol, setup_name=self.name, date=date,
                metadata={
                    "signal_type" : "sell",
                    "gap_pct"     : round(gap_pct, 3),
                    "close_pct"   : round(close_pct, 3),
                    "gap_open"    : round(float(today["open"]), 2),
                    "prev_extreme": round(float(prev["high"]), 2),
                },
            )

        return SignalResult(signal=False, symbol=symbol, setup_name=self.name, date=date,
                            metadata={"close_pct": round(close_pct, 3)})

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        """Vectorised equivalent: gap beyond prior extreme reversed into the
        opposing half of today's range."""
        import numpy as np
        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
        mid = (h + l) / 2
        long_  = (o < l.shift(1)) & (c > o) & (c > mid)
        short_ = (o > h.shift(1)) & (c < o) & (c < mid)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)
