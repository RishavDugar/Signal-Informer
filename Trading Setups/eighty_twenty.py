"""
80-20's Setup (Connors & Raschke, Chapter 6)

Detects the prior day's "80-20 bar" — a day that opened at one extreme
and closed at the opposite extreme.  The market has a high statistical
tendency to make an intraday reversal the following session.

Buy setup  : yesterday opened in the TOP 20 % of its range AND
             closed in the BOTTOM 20 % of its range.
             → Watch for today to dip below yesterday's low and reverse;
               entry buy stop at yesterday's low.

Sell setup : yesterday opened in the BOTTOM 20 % of its range AND
             closed in the TOP 80 % (i.e., top 20 %) of its range.
             → Watch for today to gap above yesterday's high and reverse.

Signal=True the morning after such a bar is detected.

Metadata:
  signal_type   : "buy" or "sell"
  open_pct      : where yesterday's open fell in the range (0–1)
  close_pct     : where yesterday's close fell in the range (0–1)
  entry_level   : yesterday's low (buy) or high (sell)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from core.base_setup import BaseSetup, SignalResult

class EightyTwentySetup(BaseSetup):
    name        = "EIGHTY_TWENTY"
    description = "Prior day opened at one range extreme and closed at the opposite (80-20 reversal bar)"
    min_periods = 2

    def __init__(self, threshold: float = 0.20):
        self.threshold = threshold

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        valid, err = self.validate_data(df)
        if not valid:
            return self._error_result(symbol, err)

        date = df.index[-1].strftime("%Y-%m-%d")
        prev = df.iloc[-2]

        day_range = prev["high"] - prev["low"]
        if day_range == 0:
            return SignalResult(signal=False, symbol=symbol,
                                setup_name=self.name, date=date,
                                metadata={"reason": "zero range on prior day"})

        open_pct  = (prev["open"]  - prev["low"]) / day_range
        close_pct = (prev["close"] - prev["low"]) / day_range
        t         = self.threshold

        # Buy setup: opened top t%, closed bottom t%
        if open_pct >= (1 - t) and close_pct <= t:
            return SignalResult(
                signal=True, symbol=symbol, setup_name=self.name, date=date,
                metadata={
                    "signal_type" : "buy",
                    "open_pct"    : round(open_pct, 3),
                    "close_pct"   : round(close_pct, 3),
                    "entry_level" : round(float(prev["low"]), 2),
                    "prev_date"   : df.index[-2].strftime("%Y-%m-%d"),
                },
            )

        # Sell setup: opened bottom t%, closed top t%
        if open_pct <= t and close_pct >= (1 - t):
            return SignalResult(
                signal=True, symbol=symbol, setup_name=self.name, date=date,
                metadata={
                    "signal_type" : "sell",
                    "open_pct"    : round(open_pct, 3),
                    "close_pct"   : round(close_pct, 3),
                    "entry_level" : round(float(prev["high"]), 2),
                    "prev_date"   : df.index[-2].strftime("%Y-%m-%d"),
                },
            )

        return SignalResult(signal=False, symbol=symbol,
                            setup_name=self.name, date=date,
                            metadata={"open_pct": round(open_pct, 3),
                                      "close_pct": round(close_pct, 3)})

    def get_stoploss(self, result, df) -> float | None:
        """Stop = prior day's low (buy) or high (sell) — the entry trigger level."""
        return float(result.metadata.get("entry_level", df["low"].iloc[-1]))
