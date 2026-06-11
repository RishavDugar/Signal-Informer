"""
2-Period ROC / Short-Term Pivot (Connors & Raschke, Chapter 8)

Calculation (daily):
  2-period ROC   = close[t] - close[t-2]
  Pivot[t]       = 2-period ROC[t] + close[t-1]
  Direction[t+1] = LONG  if close[t+1] > Pivot[t]
                   SHORT if close[t+1] < Pivot[t]

Signal fires when the direction CHANGES vs the previous session.
Use as a short-term swing bias indicator (1–2 day hold).

Metadata:
  signal_type   : "long" or "short"
  roc_2         : today's 2-period rate of change
  pivot         : today's pivot level (compare tomorrow's close to this)
  close         : today's close
  prev_direction: direction before today's flip
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from core.base_setup import BaseSetup, SignalResult


class TwoPeriodROCSetup(BaseSetup):
    name        = "TWO_PERIOD_ROC"
    description = "Short-term swing direction from 2-period ROC pivot (Taylor rhythm, 1–2 day hold)"
    min_periods = 5

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        valid, err = self.validate_data(df)
        if not valid:
            return self._error_result(symbol, err)

        date  = df.index[-1].strftime("%Y-%m-%d")
        close = df["close"]

        roc2  = close.diff(2)                      # close[t] - close[t-2]
        pivot = roc2 + close.shift(1)              # pivot used on NEXT bar

        # Today's direction: compare today's close to YESTERDAY's pivot
        def direction_at(i: int) -> int | None:
            p = pivot.iloc[i - 1]
            c = close.iloc[i]
            if pd.isna(p) or pd.isna(c):
                return None
            return 1 if c > p else -1

        today_dir = direction_at(-1)
        prev_dir  = direction_at(-2)

        if today_dir is None or prev_dir is None:
            return self._error_result(symbol, "insufficient data for pivot comparison")

        today_roc  = round(float(roc2.iloc[-1]),  2)
        today_piv  = round(float(pivot.iloc[-1]), 2)   # pivot for TOMORROW
        today_cls  = round(float(close.iloc[-1]), 2)

        changed = today_dir != prev_dir
        side    = "long" if today_dir == 1 else "short"

        return SignalResult(
            signal=changed,
            symbol=symbol,
            setup_name=self.name,
            date=date,
            metadata={
                "signal_type"   : side,
                "roc_2"         : today_roc,
                "pivot_tomorrow": today_piv,
                "close"         : today_cls,
                "direction_flip": changed,
            },
        )

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        """Vectorised equivalent: direction (close vs prior pivot) flips → ±1."""
        close = df["close"]
        roc2  = close.diff(2)
        pivot = roc2 + close.shift(1)
        prev_pivot = pivot.shift(1)
        valid = prev_pivot.notna() & close.notna()
        d     = np.where(close > prev_pivot, 1, -1)
        d_ser = pd.Series(np.where(valid, d, 0), index=df.index)
        prev  = d_ser.shift(1).fillna(0)
        flip  = (d_ser != 0) & (prev != 0) & (d_ser != prev)
        return pd.Series(np.where(flip, d_ser, 0), index=df.index)
