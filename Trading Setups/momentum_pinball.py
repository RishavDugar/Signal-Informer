"""
Momentum Pinball — LBR/RSI (Connors & Raschke, Chapter 7)

Indicator: 3-period RSI applied to the 1-period rate of change (daily net change).
Called "LBR/RSI" in the book.

Day-1 setup:
  - LBR/RSI <= 30  → buy setup  (oversold momentum)
  - LBR/RSI >= 70  → sell setup (overbought momentum)

Actual entry is on the next day's first-hour range breakout, but this setup
fires at the close of Day-1 as a heads-up.

Metadata:
  signal_type : "buy" or "sell"
  lbr_rsi     : today's LBR/RSI value
  roc_1       : today's 1-period rate of change
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from core.base_setup import BaseSetup, SignalResult

sys.path.insert(0, str(Path(__file__).parent))
from _indicators import rsi


class MomentumPinballSetup(BaseSetup):
    name        = "MOMENTUM_PINBALL"
    description = "3-period RSI of 1-day ROC < 30 (buy) or > 70 (sell) — LBR/RSI Day-1 setup"
    min_periods = 10

    def __init__(self, rsi_period: int = 3, oversold: float = 30.0, overbought: float = 70.0):
        self.rsi_period  = rsi_period
        self.oversold    = oversold
        self.overbought  = overbought

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        valid, err = self.validate_data(df)
        if not valid:
            return self._error_result(symbol, err)

        date   = df.index[-1].strftime("%Y-%m-%d")
        roc1   = df["close"].diff()                     # 1-period rate of change
        lbr    = rsi(roc1.dropna(), self.rsi_period)
        lbr    = lbr.reindex(df.index)

        val = float(lbr.iloc[-1])
        if pd.isna(val):
            return self._error_result(symbol, "LBR/RSI is NaN")

        roc_val = round(float(roc1.iloc[-1]), 2)

        if val <= self.oversold:
            return SignalResult(
                signal=True, symbol=symbol, setup_name=self.name, date=date,
                metadata={"signal_type": "buy", "lbr_rsi": round(val, 2), "roc_1": roc_val},
            )
        if val >= self.overbought:
            return SignalResult(
                signal=True, symbol=symbol, setup_name=self.name, date=date,
                metadata={"signal_type": "sell", "lbr_rsi": round(val, 2), "roc_1": roc_val},
            )

        return SignalResult(signal=False, symbol=symbol, setup_name=self.name, date=date,
                            metadata={"lbr_rsi": round(val, 2)})
