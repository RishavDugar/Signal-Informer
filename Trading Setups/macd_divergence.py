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

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        """Vectorised equivalent: price extreme beyond the prior-window extreme
        while the MACD histogram refuses to confirm (compared at the bar of the
        prior swing extreme — argmin/argmax of close in the shifted window)."""
        lb = self.lookback
        close = df["close"]
        _, _, hist = compute_macd(close, self.fast_period,
                                  self.slow_period, self.signal_period)

        win_min = close.shift(1).rolling(lb, min_periods=lb).min()
        win_max = close.shift(1).rolling(lb, min_periods=lb).max()
        amin = close.shift(1).rolling(lb, min_periods=lb).apply(np.argmin, raw=True)
        amax = close.shift(1).rolling(lb, min_periods=lb).apply(np.argmax, raw=True)

        n = len(df)
        hist_np = hist.to_numpy(dtype=float)
        idx     = np.arange(n, dtype=float)
        # absolute index of the prior swing extreme: (i - lb) + offset
        pos_min = idx - lb + amin.to_numpy(dtype=float)
        pos_max = idx - lb + amax.to_numpy(dtype=float)
        h_at_min = np.full(n, np.nan)
        h_at_max = np.full(n, np.nan)
        ok_min = ~np.isnan(pos_min)
        ok_max = ~np.isnan(pos_max)
        h_at_min[ok_min] = hist_np[pos_min[ok_min].astype(int)]
        h_at_max[ok_max] = hist_np[pos_max[ok_max].astype(int)]

        turning_up   = (hist < 0) & (hist > hist.shift(1))
        turning_down = (hist > 0) & (hist < hist.shift(1))
        long_  = turning_up   & (close < win_min) & (hist > pd.Series(h_at_min, index=df.index))
        short_ = turning_down & (close > win_max) & (hist < pd.Series(h_at_max, index=df.index))
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)
