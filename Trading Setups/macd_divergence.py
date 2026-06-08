"""
MACD Divergence

Classic momentum divergence between price extremes and the MACD histogram.

Buy signal (bullish divergence):
  1. MACD histogram is currently below zero (negative momentum zone).
  2. Histogram is turning up: hist[-1] > hist[-2].
  3. Current close is below the lowest close in the prior `lookback` bars
     (price has made a fresh lower low relative to the window).
  4. Histogram at today's bar is higher than the histogram at that prior low bar
     (momentum is not confirming the new price low — divergence).

Sell signal (bearish divergence): mirror image — price higher high, histogram
  above zero, histogram turning down, histogram lower than at prior high.

Entry: close of the divergence bar (or next open).
Stop: recent swing low (buy) / swing high (sell).

Metadata:
  signal_type : "buy" or "sell"
  macd_hist   : current histogram value
  prior_hist  : histogram at the reference swing extreme
  close       : current close
  prior_extreme: prior close (the reference swing low or high)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from core.base_setup import BaseSetup, SignalResult

sys.path.insert(0, str(Path(__file__).parent))
from _indicators import macd as compute_macd


class MacdDivergenceSetup(BaseSetup):
    name        = "MACD_DIVERGENCE"
    description = "Price lower/higher extreme while MACD histogram diverges — momentum reversal"
    min_periods = 60

    def __init__(
        self,
        fast_period:   int = 12,
        slow_period:   int = 26,
        signal_period: int = 9,
        lookback:      int = 15,
    ):
        self.fast_period   = fast_period
        self.slow_period   = slow_period
        self.signal_period = signal_period
        self.lookback      = lookback
        self.min_periods   = slow_period + signal_period + lookback + 5

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        valid, err = self.validate_data(df)
        if not valid:
            return self._error_result(symbol, err)

        date = df.index[-1].strftime("%Y-%m-%d")

        _, _, hist = compute_macd(
            df["close"], self.fast_period, self.slow_period, self.signal_period
        )

        cur_hist  = float(hist.iloc[-1])
        prev_hist = float(hist.iloc[-2])
        cur_close = float(df["close"].iloc[-1])

        if np.isnan(cur_hist) or np.isnan(prev_hist):
            return self._error_result(symbol, "MACD histogram is NaN")

        # Window of prior bars used to find the reference swing extreme.
        # Excludes the current bar so we compare current vs prior.
        w_close = df["close"].iloc[-(self.lookback + 1):-1]
        w_hist  = hist.iloc[-(self.lookback + 1):-1]

        if w_hist.isna().all():
            return self._error_result(symbol, "MACD NaN in lookback window")

        # ── Bullish divergence ────────────────────────────────────────────────
        if cur_hist < 0 and cur_hist > prev_hist:          # below zero, turning up
            ref_idx      = w_close.idxmin()                # prior swing low
            prior_low    = float(w_close.loc[ref_idx])
            prior_hist   = float(w_hist.loc[ref_idx])

            if not np.isnan(prior_hist) and cur_close < prior_low and cur_hist > prior_hist:
                return SignalResult(
                    signal=True, symbol=symbol, setup_name=self.name, date=date,
                    metadata={
                        "signal_type"  : "buy",
                        "macd_hist"    : round(cur_hist, 4),
                        "prior_hist"   : round(prior_hist, 4),
                        "close"        : round(cur_close, 2),
                        "prior_extreme": round(prior_low, 2),
                    },
                )

        # ── Bearish divergence ────────────────────────────────────────────────
        if cur_hist > 0 and cur_hist < prev_hist:          # above zero, turning down
            ref_idx      = w_close.idxmax()                # prior swing high
            prior_high   = float(w_close.loc[ref_idx])
            prior_hist   = float(w_hist.loc[ref_idx])

            if not np.isnan(prior_hist) and cur_close > prior_high and cur_hist < prior_hist:
                return SignalResult(
                    signal=True, symbol=symbol, setup_name=self.name, date=date,
                    metadata={
                        "signal_type"  : "sell",
                        "macd_hist"    : round(cur_hist, 4),
                        "prior_hist"   : round(prior_hist, 4),
                        "close"        : round(cur_close, 2),
                        "prior_extreme": round(prior_high, 2),
                    },
                )

        return SignalResult(
            signal=False, symbol=symbol, setup_name=self.name, date=date,
            metadata={"macd_hist": round(cur_hist, 4), "close": round(cur_close, 2)},
        )
