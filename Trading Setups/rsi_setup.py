"""
RSI Overbought / Oversold Setup

Signal = True when:
  - RSI > overbought threshold (default 70)  → potential reversal / trend continuation short
  - RSI < oversold threshold  (default 30)  → potential reversal / trend continuation long

Metadata:
  rsi       — current RSI value (2 dp)
  condition — "overbought" | "oversold" | "neutral"
  period    — RSI look-back period used
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Allow import of core package regardless of how this file is loaded
sys.path.insert(0, str(Path(__file__).parent.parent))
from core.base_setup import BaseSetup, SignalResult


class RSISetup(BaseSetup):
    name = "RSI_EXTREME"
    description = "Signal when RSI crosses above 70 (overbought) or below 30 (oversold)"
    min_periods = 30  # need more than `period` rows for a meaningful EWM

    def __init__(self, period: int = 14, overbought: float = 70.0, oversold: float = 30.0):
        self.period = period
        self.overbought = overbought
        self.oversold = oversold
        self.min_periods = max(self.period + 1, 30)

    # ── Private ───────────────────────────────────────────────────────────────

    def _rsi(self, closes: pd.Series) -> pd.Series:
        """Wilder's RSI using exponentially-weighted moving averages."""
        delta = closes.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        # com = period - 1 matches Wilder's smoothing (alpha = 1/period)
        avg_gain = gain.ewm(com=self.period - 1, min_periods=self.period).mean()
        avg_loss = loss.ewm(com=self.period - 1, min_periods=self.period).mean()

        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))

        # avg_loss = 0, avg_gain > 0 → pure uptrend → RSI = 100
        rsi = rsi.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
        # avg_loss = 0, avg_gain = 0 → flat/no movement → RSI = 50 (neutral)
        rsi = rsi.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)
        return rsi

    # ── Public ────────────────────────────────────────────────────────────────

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        valid, err = self.validate_data(df)
        if not valid:
            return self._error_result(symbol, err)

        rsi_series = self._rsi(df["close"])
        latest_rsi = float(rsi_series.iloc[-1])
        prev_rsi   = float(rsi_series.iloc[-2])

        if np.isnan(latest_rsi) or np.isnan(prev_rsi):
            return self._error_result(symbol, "RSI is NaN — insufficient data for EWM convergence")

        # Only fire on the bar RSI first CROSSES into the extreme zone.
        # Avoids stale multi-day signals when a stock sits at an extreme.
        is_overbought = latest_rsi >= self.overbought and prev_rsi < self.overbought
        is_oversold   = latest_rsi <= self.oversold   and prev_rsi > self.oversold
        fired         = is_overbought or is_oversold

        if is_overbought:
            condition = "overbought"
        elif is_oversold:
            condition = "oversold"
        else:
            condition = "neutral"

        latest_date = df.index[-1].strftime("%Y-%m-%d")

        return SignalResult(
            signal=fired,
            symbol=symbol,
            setup_name=self.name,
            date=latest_date,
            metadata={
                "rsi": round(latest_rsi, 2),
                "condition": condition,
                "period": self.period,
                "threshold_high": self.overbought,
                "threshold_low": self.oversold,
            },
        )
