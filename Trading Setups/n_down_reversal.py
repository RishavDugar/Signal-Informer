"""
N-Down Reversal (Connors-style streak mean reversion)

Identifies short-term seller/buyer exhaustion by counting consecutive closes
in the opposite direction of the prevailing trend.

Buy signal:
  1. Exactly `n_down` consecutive lower closes (close[t] < close[t-1] for the
     last n_down bars) — sellers are exhausted after a short-term flush.
  2. Current close is above the `trend_period`-bar SMA — the broader trend is up,
     so the down streak is a pullback, not a breakdown.

Sell signal:
  1. Exactly `n_down` consecutive higher closes — buyers exhausted.
  2. Current close is below the trend SMA — the broader trend is down.

"Exactly" means the streak is precisely n_down: the bar before the streak
started had close >= close[-n_down-1] (not also a down bar), preventing
re-fires during extended streaks.

Entry: close of the Nth consecutive bar (or next open).
Stop: prior swing high (buy) / prior swing low (sell).

Metadata:
  signal_type  : "buy" or "sell"
  streak       : consecutive bar count (always == n_down at signal)
  sma_val      : current SMA value
  close        : current close
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from core.base_setup import BaseSetup, SignalResult

sys.path.insert(0, str(Path(__file__).parent))
from _indicators import sma as compute_sma


class NDownReversalSetup(BaseSetup):
    name        = "N_DOWN_REVERSAL"
    description = "Exactly N consecutive lower closes in an uptrend — short-term exhaustion reversal"
    min_periods = 110

    def __init__(
        self,
        n_down:       int = 4,
        trend_period: int = 100,
    ):
        self.n_down       = n_down
        self.trend_period = trend_period
        self.min_periods  = trend_period + n_down + 3

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        valid, err = self.validate_data(df)
        if not valid:
            return self._error_result(symbol, err)

        date      = df.index[-1].strftime("%Y-%m-%d")
        closes    = df["close"]
        sma_s     = compute_sma(closes, self.trend_period)
        cur_sma   = float(sma_s.iloc[-1])
        cur_close = float(closes.iloc[-1])

        if np.isnan(cur_sma):
            return self._error_result(symbol, "SMA is NaN")

        n = self.n_down
        needed = n + 2           # n consecutive + 1 before streak + 1 before that

        if len(df) < needed:
            return self._error_result(symbol, f"insufficient bars for streak check ({needed} needed)")

        # Count consecutive down closes ending at current bar
        down_streak = 0
        for i in range(1, n + 2):
            if float(closes.iloc[-i]) < float(closes.iloc[-i - 1]):
                down_streak += 1
            else:
                break

        # Count consecutive up closes ending at current bar
        up_streak = 0
        for i in range(1, n + 2):
            if float(closes.iloc[-i]) > float(closes.iloc[-i - 1]):
                up_streak += 1
            else:
                break

        # ── Buy: exactly n_down consecutive down bars, in an uptrend ─────────
        if down_streak == n and cur_close > cur_sma:
            return SignalResult(
                signal=True, symbol=symbol, setup_name=self.name, date=date,
                metadata={
                    "signal_type": "buy",
                    "streak"     : n,
                    "sma_val"    : round(cur_sma, 2),
                    "close"      : round(cur_close, 2),
                },
            )

        # ── Sell: exactly n_down consecutive up bars, in a downtrend ─────────
        if up_streak == n and cur_close < cur_sma:
            return SignalResult(
                signal=True, symbol=symbol, setup_name=self.name, date=date,
                metadata={
                    "signal_type": "sell",
                    "streak"     : n,
                    "sma_val"    : round(cur_sma, 2),
                    "close"      : round(cur_close, 2),
                },
            )

        return SignalResult(
            signal=False, symbol=symbol, setup_name=self.name, date=date,
            metadata={
                "down_streak": down_streak,
                "up_streak"  : up_streak,
                "sma_val"    : round(cur_sma, 2),
            },
        )
