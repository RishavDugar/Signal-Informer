"""
ADX Gapper (Connors & Raschke, Chapter 11)

Trades a gap reversal only when a strong trend is already in place.
Uses a 12-period ADX and 28-period +DI/-DI (different periods — as specified).

Buy conditions:
  1. ADX(12) > 30
  2. +DI(28) > -DI(28)  (uptrend)
  3. Today's OPEN gaps BELOW yesterday's LOW

Sell conditions (reversed):
  1. ADX(12) > 30
  2. -DI(28) > +DI(28)  (downtrend)
  3. Today's OPEN gaps ABOVE yesterday's HIGH

Signal fires on today's close (confirming the gap occurred today).

Metadata:
  signal_type : "buy" or "sell"
  adx         : ADX(12) value
  plus_di     : +DI(28)
  minus_di    : -DI(28)
  gap_size    : distance of the gap (open - prev_low for buy, etc.)
  entry_level : yesterday's low (buy) or high (sell) — the entry buy/sell stop
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from core.base_setup import BaseSetup, SignalResult

sys.path.insert(0, str(Path(__file__).parent))
from _indicators import adx as compute_adx


class ADXGapperSetup(BaseSetup):
    name        = "ADX_GAPPER"
    description = "Gap reversal filtered by ADX(12)>30 and DI(28) trend direction"
    min_periods = 60

    def __init__(self, adx_period: int = 12, di_period: int = 28, adx_threshold: float = 30.0):
        self.adx_period    = adx_period
        self.di_period     = di_period
        self.adx_threshold = adx_threshold
        self.min_periods   = di_period * 2 + 10

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        valid, err = self.validate_data(df)
        if not valid:
            return self._error_result(symbol, err)

        date = df.index[-1].strftime("%Y-%m-%d")

        adx_s, plus_di, minus_di = compute_adx(df["high"], df["low"], df["close"],
                                                adx_period=self.adx_period,
                                                di_period=self.di_period)

        adx_val  = float(adx_s.iloc[-1])
        pdi      = float(plus_di.iloc[-1])
        mdi      = float(minus_di.iloc[-1])
        today_op = float(df["open"].iloc[-1])
        prev_lo  = float(df["low"].iloc[-2])
        prev_hi  = float(df["high"].iloc[-2])

        import math
        if any(math.isnan(v) for v in [adx_val, pdi, mdi]):
            return self._error_result(symbol, "NaN in ADX/DI")

        if adx_val <= self.adx_threshold:
            return SignalResult(signal=False, symbol=symbol,
                                setup_name=self.name, date=date,
                                metadata={"adx": round(adx_val, 2)})

        # Buy: uptrend + gap down
        if pdi > mdi and today_op < prev_lo:
            gap_size = round(prev_lo - today_op, 2)
            return SignalResult(
                signal=True, symbol=symbol, setup_name=self.name, date=date,
                metadata={
                    "signal_type" : "buy",
                    "adx"         : round(adx_val, 2),
                    "plus_di"     : round(pdi, 2),
                    "minus_di"    : round(mdi, 2),
                    "gap_size"    : gap_size,
                    "entry_level" : round(prev_lo, 2),
                },
            )

        # Sell: downtrend + gap up
        if mdi > pdi and today_op > prev_hi:
            gap_size = round(today_op - prev_hi, 2)
            return SignalResult(
                signal=True, symbol=symbol, setup_name=self.name, date=date,
                metadata={
                    "signal_type" : "sell",
                    "adx"         : round(adx_val, 2),
                    "plus_di"     : round(pdi, 2),
                    "minus_di"    : round(mdi, 2),
                    "gap_size"    : gap_size,
                    "entry_level" : round(prev_hi, 2),
                },
            )

        return SignalResult(signal=False, symbol=symbol, setup_name=self.name, date=date,
                            metadata={"adx": round(adx_val, 2),
                                      "plus_di": round(pdi, 2), "minus_di": round(mdi, 2)})

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        """Vectorised equivalent: gap against a strong DI-confirmed trend."""
        import numpy as np
        adx_s, pdi, mdi = compute_adx(df["high"], df["low"], df["close"],
                                      adx_period=self.adx_period,
                                      di_period=self.di_period)
        strong = adx_s > self.adx_threshold
        long_  = strong & (pdi > mdi) & (df["open"] < df["low"].shift(1))
        short_ = strong & (mdi > pdi) & (df["open"] > df["high"].shift(1))
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)
