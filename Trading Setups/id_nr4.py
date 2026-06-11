"""
ID/NR4 — Range Contraction (Connors & Raschke, Chapter 19)

Identifies the tightest coil condition: a day that is BOTH an Inside Day
AND the Narrowest Range of the last 4 bars.  Predicts an imminent volatility
expansion (trend day) in either direction.

Definitions:
  Inside Day (ID): today's HIGH < yesterday's HIGH
                   AND today's LOW > yesterday's LOW
  NR4           : today's range (H-L) is the narrowest of the last 4 bars

Entry (next day):
  Place a buy stop one tick ABOVE today's high
  AND a sell stop one tick BELOW today's low simultaneously.
  Whichever fires first initiates the position; immediately reverse
  with a stop at the other extreme if stopped out.

Signal fires on close of the ID/NR4 day.

Metadata:
  is_inside_day   : True/False
  is_nr4          : True/False
  today_high      : today's high (buy stop above this)
  today_low       : today's low  (sell stop below this)
  range_size      : today's range
  range_4bar_rank : rank of today's range within the 4-bar window (1=narrowest)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from core.base_setup import BaseSetup, SignalResult

sys.path.insert(0, str(Path(__file__).parent))
from _indicators import is_inside_day, is_nr4


class IDNr4Setup(BaseSetup):
    name        = "ID_NR4"
    description = "Inside Day + Narrowest Range of 4 bars — imminent volatility expansion"
    min_periods = 6

    def __init__(self, require_both: bool = True):
        """
        require_both=True  → signal only when BOTH inside AND NR4 (strict ID/NR4)
        require_both=False → signal when EITHER condition holds
        """
        self.require_both = require_both

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        valid, err = self.validate_data(df)
        if not valid:
            return self._error_result(symbol, err)

        date = df.index[-1].strftime("%Y-%m-%d")

        id_flag  = bool(is_inside_day(df["high"], df["low"]).iloc[-1])
        nr4_flag = bool(is_nr4(df["high"], df["low"]).iloc[-1])

        fired = (id_flag and nr4_flag) if self.require_both else (id_flag or nr4_flag)

        daily_range  = float(df["high"].iloc[-1] - df["low"].iloc[-1])
        window_ranks = (df["high"] - df["low"]).iloc[-4:]
        rank         = int(window_ranks.rank().iloc[-1])   # 1 = narrowest

        return SignalResult(
            signal=fired,
            symbol=symbol,
            setup_name=self.name,
            date=date,
            metadata={
                "is_inside_day"  : id_flag,
                "is_nr4"         : nr4_flag,
                "today_high"     : round(float(df["high"].iloc[-1]), 2),
                "today_low"      : round(float(df["low"].iloc[-1]), 2),
                "range_size"     : round(daily_range, 2),
                "range_4bar_rank": rank,
            },
        )

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        """Vectorised equivalent: ID/NR4 coil (direction-neutral → +1, treated
        as the long/breakout book in the backtester, matching _direction_of)."""
        import numpy as np
        id_s  = is_inside_day(df["high"], df["low"])
        nr4_s = is_nr4(df["high"], df["low"])
        fired = (id_s & nr4_s) if self.require_both else (id_s | nr4_s)
        return pd.Series(np.where(fired, 1, 0), index=df.index)
