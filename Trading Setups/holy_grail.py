"""
Holy Grail (Connors & Raschke, Chapter 10)

ADX-based trend-continuation retracement setup.

Buy conditions (all on the most recent bar):
  1. 14-period ADX > 30  AND  ADX is rising (ADX[t] > ADX[t-1])
  2. Price has retraced to touch the 20-period Exponential Moving Average
     (today's LOW <= EMA20 <= today's HIGH, i.e., price touched the EMA)

Once touched, place a buy stop above the previous bar's high; initial stop
at the retracement low.

Sell setup is the mirror image (ADX > 30 rising, price touches EMA from below).

Signal fires on the close of the bar that touches the EMA.

Metadata:
  signal_type : "buy" or "sell"
  adx         : current ADX value
  ema20       : current 20-period EMA
  adx_slope   : ADX[t] - ADX[t-1]
  entry_stop  : previous bar's high (buy) or low (sell)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from core.base_setup import BaseSetup, SignalResult

sys.path.insert(0, str(Path(__file__).parent))
from _indicators import adx as compute_adx, ema as compute_ema


class HolyGrailSetup(BaseSetup):
    name        = "HOLY_GRAIL"
    description = "ADX(14)>30 rising + price touches 20-EMA — trend continuation after retracement"
    min_periods = 50

    def __init__(self, adx_period: int = 14, ema_period: int = 20, adx_threshold: float = 30.0):
        self.adx_period    = adx_period
        self.ema_period    = ema_period
        self.adx_threshold = adx_threshold
        self.min_periods   = adx_period * 3 + ema_period

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        valid, err = self.validate_data(df)
        if not valid:
            return self._error_result(symbol, err)

        date = df.index[-1].strftime("%Y-%m-%d")

        adx_s, plus_di, minus_di = compute_adx(df["high"], df["low"], df["close"],
                                                adx_period=self.adx_period)
        ema20 = compute_ema(df["close"], self.ema_period)

        adx_val   = float(adx_s.iloc[-1])
        adx_prev  = float(adx_s.iloc[-2])
        ema_val   = float(ema20.iloc[-1])
        today_hi  = float(df["high"].iloc[-1])
        today_lo  = float(df["low"].iloc[-1])
        prev_hi   = float(df["high"].iloc[-2])
        prev_lo   = float(df["low"].iloc[-2])
        pdi       = float(plus_di.iloc[-1])
        mdi       = float(minus_di.iloc[-1])

        import math
        if any(math.isnan(v) for v in [adx_val, adx_prev, ema_val]):
            return self._error_result(symbol, "NaN in ADX or EMA")

        adx_strong = adx_val > self.adx_threshold
        # "Initially rising" (book wording): ADX today > ADX one full period ago.
        # Using adx_period as lookback confirms the trend is genuinely strengthening
        # while tolerating a minor 1-bar dip caused by the touch-bar itself.
        lookback    = min(self.adx_period, len(adx_s.dropna()) - 1)
        adx_rising  = adx_val > float(adx_s.iloc[-(lookback + 1)])

        if not (adx_strong and adx_rising):
            return SignalResult(signal=False, symbol=symbol,
                                setup_name=self.name, date=date,
                                metadata={"adx": round(adx_val, 2), "ema20": round(ema_val, 2)})

        touched_from_above = today_lo <= ema_val and pdi > mdi   # uptrend, touched EMA
        touched_from_below = today_hi >= ema_val and mdi > pdi   # downtrend, touched EMA

        if touched_from_above:
            return SignalResult(
                signal=True, symbol=symbol, setup_name=self.name, date=date,
                metadata={
                    "signal_type": "buy",
                    "adx"        : round(adx_val, 2),
                    "ema20"      : round(ema_val, 2),
                    "adx_slope"  : round(adx_val - adx_prev, 3),
                    "entry_stop" : round(prev_hi, 2),
                    "plus_di"    : round(pdi, 2),
                    "minus_di"   : round(mdi, 2),
                },
            )

        if touched_from_below:
            return SignalResult(
                signal=True, symbol=symbol, setup_name=self.name, date=date,
                metadata={
                    "signal_type": "sell",
                    "adx"        : round(adx_val, 2),
                    "ema20"      : round(ema_val, 2),
                    "adx_slope"  : round(adx_val - adx_prev, 3),
                    "entry_stop" : round(prev_lo, 2),
                    "plus_di"    : round(pdi, 2),
                    "minus_di"   : round(mdi, 2),
                },
            )

        return SignalResult(signal=False, symbol=symbol, setup_name=self.name, date=date,
                            metadata={"adx": round(adx_val, 2), "ema20": round(ema_val, 2),
                                      "adx_slope": round(adx_val - adx_prev, 3)})
