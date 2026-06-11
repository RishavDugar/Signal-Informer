"""
Bollinger Squeeze Breakout

Captures volatility expansion following a compression phase.

Setup conditions (both must be true on the signal bar):
  1. Squeeze active: the current Bollinger bandwidth is at its lowest point
     in the past `squeeze_lookback` bars.
     bandwidth = (upper - lower) / middle  (a ratio, not percent)
  2. Directional breakout:
     - close > upper band → buy signal  (expansion to the upside)
     - close < lower band → sell signal (expansion to the downside)

The squeeze confirms the market has coiled; the breakout bar is the entry.

Entry: close of the breakout bar.
Stop: opposite band at time of entry.

Metadata:
  signal_type       : "buy" or "sell"
  bandwidth         : current bandwidth ratio
  bandwidth_min     : N-period minimum (squeeze threshold hit)
  close             : current close
  upper / lower     : current band levels
  middle            : current moving average
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from core.base_setup import BaseSetup, SignalResult

sys.path.insert(0, str(Path(__file__).parent))
from _indicators import bollinger_bands


class BollingerSqueezeSetup(BaseSetup):
    name        = "BOLLINGER_SQUEEZE"
    description = "Band width at N-period low then close breaks outside — volatility expansion"
    min_periods = 50

    def __init__(
        self,
        period:           int   = 20,
        num_stddev:       float = 2.0,
        squeeze_lookback: int   = 20,
    ):
        self.period           = period
        self.num_stddev       = num_stddev
        self.squeeze_lookback = squeeze_lookback
        self.min_periods      = period + squeeze_lookback + 5

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        valid, err = self.validate_data(df)
        if not valid:
            return self._error_result(symbol, err)

        date = df.index[-1].strftime("%Y-%m-%d")

        upper, middle, lower = bollinger_bands(df["close"], self.period, self.num_stddev)

        cur_upper  = float(upper.iloc[-1])
        cur_middle = float(middle.iloc[-1])
        cur_lower  = float(lower.iloc[-1])
        cur_close  = float(df["close"].iloc[-1])

        if np.isnan(cur_upper) or np.isnan(cur_middle) or cur_middle == 0:
            return self._error_result(symbol, "Bollinger bands are NaN or zero midline")

        bandwidth = (upper - lower) / middle.replace(0, np.nan)
        cur_bw    = float(bandwidth.iloc[-1])

        if np.isnan(cur_bw):
            return self._error_result(symbol, "bandwidth is NaN")

        # Squeeze: the recent compression period (squeeze_lookback bars before current)
        # must contain a bandwidth minimum at least as low as the broader comparison
        # window (squeeze_lookback + period bars before current).
        # This separates the "was compressed" condition from the current breakout bar,
        # which by definition has a WIDER band than the compression bars.
        bw_excl         = bandwidth.iloc[:-1]                               # all bars except current
        bw_recent       = bw_excl.iloc[-self.squeeze_lookback:]             # recent N bars
        bw_comparison   = bw_excl.iloc[-self.squeeze_lookback - self.period:]  # longer baseline
        bw_min_recent   = float(bw_recent.min())
        bw_min_baseline = float(bw_comparison.min())
        had_squeeze     = bw_min_recent <= bw_min_baseline + 1e-10

        if not had_squeeze:
            return SignalResult(
                signal=False, symbol=symbol, setup_name=self.name, date=date,
                metadata={"bandwidth": round(cur_bw, 4), "min_recent": round(bw_min_recent, 4)},
            )

        if cur_close > cur_upper:
            return SignalResult(
                signal=True, symbol=symbol, setup_name=self.name, date=date,
                metadata={
                    "signal_type": "buy",
                    "bandwidth"  : round(cur_bw, 4),
                    "min_recent" : round(bw_min_recent, 4),
                    "close"      : round(cur_close, 2),
                    "upper"      : round(cur_upper, 2),
                    "lower"      : round(cur_lower, 2),
                    "middle"     : round(cur_middle, 2),
                },
            )

        if cur_close < cur_lower:
            return SignalResult(
                signal=True, symbol=symbol, setup_name=self.name, date=date,
                metadata={
                    "signal_type": "sell",
                    "bandwidth"  : round(cur_bw, 4),
                    "min_recent" : round(bw_min_recent, 4),
                    "close"      : round(cur_close, 2),
                    "upper"      : round(cur_upper, 2),
                    "lower"      : round(cur_lower, 2),
                    "middle"     : round(cur_middle, 2),
                },
            )

        return SignalResult(
            signal=False, symbol=symbol, setup_name=self.name, date=date,
            metadata={"bandwidth": round(cur_bw, 4), "min_recent": round(bw_min_recent, 4)},
        )

    def get_stoploss(self, result, df) -> float | None:
        """Stop = opposite Bollinger band at time of signal (book rule)."""
        meta = result.metadata
        if meta.get("signal_type") == "buy":
            return float(meta["lower"])
        return float(meta["upper"])

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        """Vectorised equivalent: recent bandwidth min at/below the longer
        baseline min (squeeze existed), then close escapes a band."""
        upper, middle, lower = bollinger_bands(df["close"], self.period, self.num_stddev)
        bw = (upper - lower) / middle.replace(0, np.nan)
        bw_prev   = bw.shift(1)
        recent    = bw_prev.rolling(self.squeeze_lookback,
                                    min_periods=self.squeeze_lookback).min()
        baseline  = bw_prev.rolling(self.squeeze_lookback + self.period,
                                    min_periods=self.squeeze_lookback + self.period).min()
        had_squeeze = recent <= baseline + 1e-10
        long_  = had_squeeze & (df["close"] > upper)
        short_ = had_squeeze & (df["close"] < lower)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)

    def vector_stops(self, df: pd.DataFrame) -> pd.Series:
        """Stop = opposite band at signal time (book rule)."""
        upper, _, lower = bollinger_bands(df["close"], self.period, self.num_stddev)
        dirs = self.vector_signals(df)
        return pd.Series(
            np.where(dirs > 0, lower, np.where(dirs < 0, upper, np.nan)),
            index=df.index)
