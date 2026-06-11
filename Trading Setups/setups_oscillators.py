"""
Oscillator & hybrid system strategy catalogue (vectorised).

Direction convention: +1 long, -1 short/sell, 0 nothing.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.vector_setup import VectorSetup

sys.path.insert(0, str(Path(__file__).parent))
from _indicators import (
    sma, ema, rsi, macd, stoch_rsi, ultimate_oscillator, cmo, dpo, trix,
    ppo, bollinger_bands, crossed_above, crossed_below,
)


class StochRSICross(VectorSetup):
    """StochRSI %K crossing %D inside an extreme zone — a fast, sensitive
    mean-reversion trigger on doubly-normalised momentum."""
    name = "STOCH_RSI_CROSS"
    description = "StochRSI K/D cross inside extreme zone"
    param_grid = {"rsi_p": [14], "stoch_p": [14], "lo": [20, 25], "hi": [75, 80]}

    def __init__(self, rsi_p: int = 14, stoch_p: int = 14,
                 lo: float = 20.0, hi: float = 80.0):
        self.rsi_p = rsi_p
        self.stoch_p = stoch_p
        self.lo = lo
        self.hi = hi
        self.min_periods = rsi_p + stoch_p + 12

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        k, d = stoch_rsi(df["close"], self.rsi_p, self.stoch_p)
        long_  = crossed_above(k, d) & (d < self.lo)
        short_ = crossed_below(k, d) & (d > self.hi)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class UltimateOscReversal(VectorSetup):
    """Ultimate Oscillator (multi-timeframe buying pressure) leaving an extreme
    zone — Williams' design avoids single-period divergence traps."""
    name = "ULTIMATE_OSC"
    description = "Ultimate oscillator extreme exit"
    param_grid = {"lo": [25, 30, 35], "hi": [65, 70, 75]}

    def __init__(self, lo: float = 30.0, hi: float = 70.0):
        self.lo = lo
        self.hi = hi
        self.min_periods = 40

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        u = ultimate_oscillator(df["high"], df["low"], df["close"])
        long_  = crossed_above(u, self.lo)   # rising back out of oversold
        short_ = crossed_below(u, self.hi)   # falling back out of overbought
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class CMOExtreme(VectorSetup):
    """Chande Momentum Oscillator beyond ±threshold then turning — CMO uses
    both up and down momentum so its extremes are harder to reach than RSI's."""
    name = "CMO_EXTREME"
    description = "Chande momentum oscillator extreme turn"
    param_grid = {"period": [9, 14, 20], "threshold": [40, 50, 60]}

    def __init__(self, period: int = 14, threshold: float = 50.0):
        self.period = period
        self.threshold = threshold
        self.min_periods = period + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        m = cmo(df["close"], self.period)
        long_  = (m.shift(1) < -self.threshold) & (m > m.shift(1))
        short_ = (m.shift(1) >  self.threshold) & (m < m.shift(1))
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class DPOReversion(VectorSetup):
    """Detrended price oscillator stretched beyond k× its own rolling std —
    cycle-relative overextension with the trend removed."""
    name = "DPO_REVERSION"
    description = "Detrended price oscillator sigma-stretch fade"
    param_grid = {"period": [20, 30], "k": [1.5, 2.0, 2.5]}

    def __init__(self, period: int = 20, k: float = 2.0):
        self.period = period
        self.k = k
        self.min_periods = period * 2 + 20

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        d  = dpo(df["close"], self.period)
        sd = d.rolling(self.period * 2, min_periods=self.period * 2).std()
        long_  = crossed_below(d, -self.k * sd)
        short_ = crossed_above(d,  self.k * sd)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class TrixZeroCross(VectorSetup):
    """TRIX (triple-smoothed momentum) crossing its signal line — almost all
    one-bar noise is filtered out before the cross can happen."""
    name = "TRIX_CROSS"
    description = "TRIX signal-line cross"
    param_grid = {"period": [9, 15], "signal_p": [6, 9]}

    def __init__(self, period: int = 15, signal_p: int = 9):
        self.period = period
        self.signal_p = signal_p
        self.min_periods = period * 3 + signal_p + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        t, sig = trix(df["close"], self.period, self.signal_p)
        long_  = crossed_above(t, sig)
        short_ = crossed_below(t, sig)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class PPOMomentum(VectorSetup):
    """PPO histogram flipping positive with PPO above zero — percentage-scaled
    MACD that compares cleanly across price levels."""
    name = "PPO_MOMENTUM"
    description = "PPO histogram flip in the direction of the PPO regime"
    param_grid = {"fast": [12], "slow": [26], "signal_p": [9]}

    def __init__(self, fast: int = 12, slow: int = 26, signal_p: int = 9):
        self.fast = fast
        self.slow = slow
        self.signal_p = signal_p
        self.min_periods = slow + signal_p + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        p, sig, hist = ppo(df["close"], self.fast, self.slow, self.signal_p)
        long_  = crossed_above(hist, 0.0) & (p > 0)
        short_ = crossed_below(hist, 0.0) & (p < 0)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class ElderImpulse(VectorSetup):
    """Elder Impulse: EMA(13) slope AND MACD histogram slope agree — inertia
    plus momentum. Fire on the first bar both turn green (long) / red (short)."""
    name = "ELDER_IMPULSE"
    description = "Elder impulse system colour change"
    param_grid = {"ema_period": [13, 21]}

    def __init__(self, ema_period: int = 13):
        self.ema_period = ema_period
        self.min_periods = 26 + 9 + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        e = ema(df["close"], self.ema_period)
        _, _, hist = macd(df["close"])
        green = (e > e.shift(1)) & (hist > hist.shift(1))
        red   = (e < e.shift(1)) & (hist < hist.shift(1))
        long_  = green & ~green.shift(1).fillna(False)
        short_ = red   & ~red.shift(1).fillna(False)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class RSIBollingerConfluence(VectorSetup):
    """Confluence: RSI oversold AND close below the lower Bollinger band on the
    same bar — two independent definitions of 'too far, too fast' agreeing."""
    name = "RSI_BB_CONFLUENCE"
    description = "RSI extreme + Bollinger band breach confluence"
    param_grid = {"rsi_period": [7, 14], "rsi_lo": [25, 30], "rsi_hi": [70, 75],
                  "bb_period": [20]}

    def __init__(self, rsi_period: int = 14, rsi_lo: float = 30.0,
                 rsi_hi: float = 70.0, bb_period: int = 20, bb_std: float = 2.0):
        self.rsi_period = rsi_period
        self.rsi_lo = rsi_lo
        self.rsi_hi = rsi_hi
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.min_periods = max(rsi_period, bb_period) + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        r = rsi(df["close"], self.rsi_period)
        upper, _, lower = bollinger_bands(df["close"], self.bb_period, self.bb_std)
        long_  = (r < self.rsi_lo) & (df["close"] < lower)
        short_ = (r > self.rsi_hi) & (df["close"] > upper)
        # first bar of confluence only
        long_  &= ~(long_.shift(1).fillna(False))
        short_ &= ~(short_.shift(1).fillna(False))
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)
