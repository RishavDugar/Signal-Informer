"""
Historical Volatility + Crabel NR4/Inside Day (Connors & Raschke, Chapter 20)

Combines Toby Crabel's range contraction with a historical volatility filter
to pinpoint explosive moves from unusually quiet conditions.

Conditions:
  1. 6-day historical volatility < 50 % of 100-day historical volatility
     (price range has contracted to less than half its long-run average)
  2. Today is an Inside Day OR an NR4 day

Entry (next day):
  Place buy stop one tick above today's high
  AND sell stop one tick below today's low.
  If filled, add a reversal stop on the OTHER side (day-of-entry only).

Signal fires on close when both conditions hold.

Metadata:
  hv6          : 6-day annualised historical volatility
  hv100        : 100-day annualised historical volatility
  hv_ratio     : hv6 / hv100 (< 0.5 triggers)
  is_inside_day: True/False
  is_nr4       : True/False
  today_high   : entry buy-stop reference
  today_low    : entry sell-stop reference
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import math
import pandas as pd
from core.base_setup import BaseSetup, SignalResult

sys.path.insert(0, str(Path(__file__).parent))
from _indicators import historical_vol, is_inside_day, is_nr4


class HVNr4Setup(BaseSetup):
    name        = "HV_NR4"
    description = "6-day HV < 50% of 100-day HV + Inside Day or NR4 — ultra-low volatility breakout"
    min_periods = 110

    def __init__(self, short_period: int = 6, long_period: int = 100,
                 hv_ratio_threshold: float = 0.50):
        self.short_period        = short_period
        self.long_period         = long_period
        self.hv_ratio_threshold  = hv_ratio_threshold
        self.min_periods         = long_period + 10

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        valid, err = self.validate_data(df)
        if not valid:
            return self._error_result(symbol, err)

        date = df.index[-1].strftime("%Y-%m-%d")

        hv_short = historical_vol(df["close"], self.short_period)
        hv_long  = historical_vol(df["close"], self.long_period)

        hv6   = float(hv_short.iloc[-1])
        hv100 = float(hv_long.iloc[-1])

        if math.isnan(hv6) or math.isnan(hv100) or hv100 == 0:
            return self._error_result(symbol, "NaN or zero in historical volatility")

        hv_ratio = hv6 / hv100
        id_flag  = bool(is_inside_day(df["high"], df["low"]).iloc[-1])
        nr4_flag = bool(is_nr4(df["high"], df["low"]).iloc[-1])

        fired = (hv_ratio < self.hv_ratio_threshold) and (id_flag or nr4_flag)

        return SignalResult(
            signal=fired,
            symbol=symbol,
            setup_name=self.name,
            date=date,
            metadata={
                "hv6"          : round(hv6,      4),
                "hv100"        : round(hv100,    4),
                "hv_ratio"     : round(hv_ratio, 3),
                "is_inside_day": id_flag,
                "is_nr4"       : nr4_flag,
                "today_high"   : round(float(df["high"].iloc[-1]), 2),
                "today_low"    : round(float(df["low"].iloc[-1]), 2),
                "range_size"   : round(float(df["high"].iloc[-1] - df["low"].iloc[-1]), 2),
            },
        )
