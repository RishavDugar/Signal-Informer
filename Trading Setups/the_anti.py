"""
The "Anti" (Connors & Raschke, Chapter 9)

A retracement-and-hook setup using a modified Stochastic oscillator.

Parameters (per the book):
  %K period = 7 (fast stochastic)
  %D period = 10 (slow moving average of %K)

Buy setup conditions (all must hold on today's bar):
  1. %D slope is POSITIVE (%D[t] > %D[t-1])           — longer-term upward momentum
  2. %K has been declining for at least 1 bar           — retracement underway
  3. %K just hooked UP: %K[t] > %K[t-1] AND            — hook in direction of %D
                        %K[t-1] < %K[t-2]

Sell setup is the mirror image.

Aggressive entry: break of a trendline across the retracement highs/lows.
Conservative entry: open the morning after the hook forms.

Signal fires on the close when the hook is detected.

Metadata:
  signal_type : "buy" or "sell"
  pct_k       : current %K
  pct_d       : current %D
  d_slope     : %D change today
  hook_bar    : date the hook formed (today)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from core.base_setup import BaseSetup, SignalResult

sys.path.insert(0, str(Path(__file__).parent))
from _indicators import stochastic


class TheAntiSetup(BaseSetup):
    name        = "THE_ANTI"
    description = "Stochastic hook in direction of %D trend — retracement continuation setup"
    min_periods = 25

    def __init__(self, k_period: int = 7, d_period: int = 10):
        self.k_period = k_period
        self.d_period = d_period
        self.min_periods = k_period + d_period + 5

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        valid, err = self.validate_data(df)
        if not valid:
            return self._error_result(symbol, err)

        date          = df.index[-1].strftime("%Y-%m-%d")
        pct_k, pct_d  = stochastic(df["high"], df["low"], df["close"],
                                    self.k_period, self.d_period)

        k0, k1, k2 = float(pct_k.iloc[-1]), float(pct_k.iloc[-2]), float(pct_k.iloc[-3])
        d0, d1     = float(pct_d.iloc[-1]), float(pct_d.iloc[-2])

        import math
        if any(math.isnan(v) for v in [k0, k1, k2, d0, d1]):
            return self._error_result(symbol, "NaN in stochastic values")

        d_slope    = d0 - d1
        hook_up    = (k0 > k1) and (k1 < k2)   # K turned up after declining
        hook_down  = (k0 < k1) and (k1 > k2)   # K turned down after rising

        # Buy: %D trending up AND hook up in %K
        if d_slope > 0 and hook_up:
            return SignalResult(
                signal=True, symbol=symbol, setup_name=self.name, date=date,
                metadata={
                    "signal_type": "buy",
                    "pct_k"      : round(k0, 2),
                    "pct_d"      : round(d0, 2),
                    "d_slope"    : round(d_slope, 3),
                },
            )

        # Sell: %D trending down AND hook down in %K
        if d_slope < 0 and hook_down:
            return SignalResult(
                signal=True, symbol=symbol, setup_name=self.name, date=date,
                metadata={
                    "signal_type": "sell",
                    "pct_k"      : round(k0, 2),
                    "pct_d"      : round(d0, 2),
                    "d_slope"    : round(d_slope, 3),
                },
            )

        return SignalResult(signal=False, symbol=symbol, setup_name=self.name, date=date,
                            metadata={"pct_k": round(k0, 2), "pct_d": round(d0, 2),
                                      "d_slope": round(d_slope, 3)})

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        """Vectorised equivalent: %K hook in the direction of the %D slope."""
        import numpy as np
        k, d = stochastic(df["high"], df["low"], df["close"],
                          self.k_period, self.d_period)
        d_slope   = d - d.shift(1)
        hook_up   = (k > k.shift(1)) & (k.shift(1) < k.shift(2))
        hook_down = (k < k.shift(1)) & (k.shift(1) > k.shift(2))
        long_  = (d_slope > 0) & hook_up
        short_ = (d_slope < 0) & hook_down
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)
