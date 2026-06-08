"""
Volume Climax Reversal

Identifies exhaustion bars: high volume + wide range + close at one extreme.
These candles signal that the dominant side (buyers or sellers) has spent itself.

Buy signal (selling climax):
  1. Volume > vol_multiplier × average volume over vol_period bars.
  2. Bar range (high - low) > atr_multiplier × ATR(atr_period).
  3. Close is in the LOWER close_threshold of the bar's range:
       close <= low + close_threshold × (high - low)
  A wide, high-volume down bar that closes near the lows signals seller exhaustion.

Sell signal (buying climax): mirror image — close is in the UPPER close_threshold,
  wide high-volume bar implies buyer exhaustion.

Entry: next bar's open (signal fires at the close of the climax bar).
Stop: low of the climax bar (buy) / high of the climax bar (sell).

Metadata:
  signal_type     : "buy" or "sell"
  volume_ratio    : volume / average_volume
  range_vs_atr    : range / ATR
  close_position  : where close sits in the bar (0 = at low, 1 = at high)
  close           : current close
  vol_avg         : rolling average volume
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from core.base_setup import BaseSetup, SignalResult

sys.path.insert(0, str(Path(__file__).parent))
from _indicators import atr as compute_atr


class VolumeClimaxSetup(BaseSetup):
    name        = "VOLUME_CLIMAX"
    description = "High-volume wide-range bar with close at extreme — exhaustion reversal"
    min_periods = 35

    def __init__(
        self,
        vol_period:      int   = 20,
        vol_multiplier:  float = 2.0,
        atr_period:      int   = 14,
        atr_multiplier:  float = 2.0,
        close_threshold: float = 0.25,
    ):
        self.vol_period      = vol_period
        self.vol_multiplier  = vol_multiplier
        self.atr_period      = atr_period
        self.atr_multiplier  = atr_multiplier
        self.close_threshold = close_threshold
        self.min_periods     = max(vol_period, atr_period) + 5

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        valid, err = self.validate_data(df)
        if not valid:
            return self._error_result(symbol, err)

        date = df.index[-1].strftime("%Y-%m-%d")

        cur_high   = float(df["high"].iloc[-1])
        cur_low    = float(df["low"].iloc[-1])
        cur_close  = float(df["close"].iloc[-1])
        cur_volume = float(df["volume"].iloc[-1])

        # Volume spike
        vol_avg = float(
            df["volume"].iloc[-(self.vol_period + 1):-1].mean()
        )
        if vol_avg == 0 or np.isnan(vol_avg):
            return self._error_result(symbol, "zero or NaN average volume")

        volume_ratio = cur_volume / vol_avg
        if volume_ratio < self.vol_multiplier:
            return SignalResult(
                signal=False, symbol=symbol, setup_name=self.name, date=date,
                metadata={"volume_ratio": round(volume_ratio, 2)},
            )

        # Wide range
        atr_s   = compute_atr(df["high"], df["low"], df["close"], self.atr_period)
        cur_atr = float(atr_s.iloc[-1])
        if np.isnan(cur_atr) or cur_atr == 0:
            return self._error_result(symbol, "ATR is NaN or zero")

        bar_range    = cur_high - cur_low
        range_vs_atr = bar_range / cur_atr
        if range_vs_atr < self.atr_multiplier:
            return SignalResult(
                signal=False, symbol=symbol, setup_name=self.name, date=date,
                metadata={
                    "volume_ratio": round(volume_ratio, 2),
                    "range_vs_atr": round(range_vs_atr, 2),
                },
            )

        # Close position within bar (0 = at low, 1 = at high)
        if bar_range == 0:
            return SignalResult(signal=False, symbol=symbol, setup_name=self.name, date=date,
                                metadata={"volume_ratio": round(volume_ratio, 2)})

        close_pos = (cur_close - cur_low) / bar_range

        base_meta = {
            "volume_ratio": round(volume_ratio, 2),
            "range_vs_atr": round(range_vs_atr, 2),
            "close_position": round(close_pos, 3),
            "close"         : round(cur_close, 2),
            "vol_avg"       : round(vol_avg, 0),
        }

        # Selling climax → buy
        if close_pos <= self.close_threshold:
            return SignalResult(
                signal=True, symbol=symbol, setup_name=self.name, date=date,
                metadata={"signal_type": "buy", **base_meta},
            )

        # Buying climax → sell
        if close_pos >= 1 - self.close_threshold:
            return SignalResult(
                signal=True, symbol=symbol, setup_name=self.name, date=date,
                metadata={"signal_type": "sell", **base_meta},
            )

        return SignalResult(signal=False, symbol=symbol, setup_name=self.name, date=date,
                            metadata=base_meta)
