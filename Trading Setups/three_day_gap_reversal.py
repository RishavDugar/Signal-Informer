"""
Three-Day Unfilled Gap Reversal (Connors & Raschke, Chapter 13)

A gap that forms and remains UNFILLED has strong reversal potential.

Buy setup:
  - Gap-down bar: today's OPEN < yesterday's LOW
  - Gap NOT filled on the gap day: today's HIGH < yesterday's LOW
  - Entry buy stop: ONE TICK above the gap-day's HIGH
  - Validity: next 3 trading sessions only; cancel if not triggered

Sell setup (reversed):
  - Gap-up bar: OPEN > yesterday's HIGH
  - Gap not filled: LOW > yesterday's HIGH
  - Entry sell stop: one tick below the gap-day's LOW

Signal fires ONLY on the close of the gap bar itself (yesterday's bar).
The 3-session validity window is for entry management (keep the stop
order live for 3 days), not for re-signalling — counting one gap event
as three separate backtester trades inflates the sample size by 3×.

Metadata:
  signal_type   : "buy" or "sell"
  gap_day       : date the gap occurred (always yesterday)
  gap_level     : entry stop level (gap-day high for buy, low for sell)
  days_since_gap: always 1
  still_open    : True (gap still unfilled as of today)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from core.base_setup import BaseSetup, SignalResult


class ThreeDayGapReversalSetup(BaseSetup):
    name        = "THREE_DAY_GAP_REVERSAL"
    description = "Unfilled gap within 3 sessions — anticipate gap-fill reversal"
    min_periods = 5
    MAX_DAYS    = 3

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        valid, err = self.validate_data(df)
        if not valid:
            return self._error_result(symbol, err)

        date = df.index[-1].strftime("%Y-%m-%d")

        # Only fire on the gap bar itself (lag=1 = gap happened yesterday).
        # The 3-day validity window is for keeping the entry stop active;
        # re-firing on days 2 and 3 counts one gap event as three trades.
        for lag in range(1, 2):
            if lag + 1 >= len(df):
                break

            gap_bar  = df.iloc[-(lag + 1)]   # the potential gap day
            prev_bar = df.iloc[-(lag + 2)]   # day before the gap

            # ── Buy: gap down, unfilled ──────────────────────────────────
            gap_fill_level = float(prev_bar["low"])
            if (float(gap_bar["open"]) < gap_fill_level
                    and float(gap_bar["high"]) < gap_fill_level):

                # Check every bar AFTER the gap day: still unfilled?
                still_open = True
                for k in range(lag - 1, -1, -1):
                    subsequent = df.iloc[-(k + 1)]
                    if float(subsequent["high"]) >= gap_fill_level:
                        still_open = False
                        break

                if still_open:
                    return SignalResult(
                        signal=True, symbol=symbol, setup_name=self.name, date=date,
                        metadata={
                            "signal_type"   : "buy",
                            "gap_day"       : df.index[-(lag + 1)].strftime("%Y-%m-%d"),
                            "gap_level"     : round(float(gap_bar["high"]), 2),
                            "fill_level"    : round(gap_fill_level, 2),
                            "days_since_gap": lag,
                            "still_open"    : True,
                        },
                    )

            # ── Sell: gap up, unfilled ────────────────────────────────────
            gap_fill_level_sell = float(prev_bar["high"])
            if (float(gap_bar["open"]) > gap_fill_level_sell
                    and float(gap_bar["low"]) > gap_fill_level_sell):

                still_open = True
                for k in range(lag - 1, -1, -1):
                    subsequent = df.iloc[-(k + 1)]
                    if float(subsequent["low"]) <= gap_fill_level_sell:
                        still_open = False
                        break

                if still_open:
                    return SignalResult(
                        signal=True, symbol=symbol, setup_name=self.name, date=date,
                        metadata={
                            "signal_type"   : "sell",
                            "gap_day"       : df.index[-(lag + 1)].strftime("%Y-%m-%d"),
                            "gap_level"     : round(float(gap_bar["low"]), 2),
                            "fill_level"    : round(gap_fill_level_sell, 2),
                            "days_since_gap": lag,
                            "still_open"    : True,
                        },
                    )

        return SignalResult(signal=False, symbol=symbol, setup_name=self.name, date=date,
                            metadata={"reason": "no qualifying unfilled gap in last 3 bars"})

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        """Vectorised equivalent: yesterday was an unfilled gap bar (gap not
        filled on the gap day itself); fires on the following bar."""
        import numpy as np
        o, h, l = df["open"], df["high"], df["low"]
        # gap yesterday, unfilled on the gap day AND still unfilled today
        long_  = (o.shift(1) < l.shift(2)) & (h.shift(1) < l.shift(2)) & (h < l.shift(2))
        short_ = (o.shift(1) > h.shift(2)) & (l.shift(1) > h.shift(2)) & (l > h.shift(2))
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)
