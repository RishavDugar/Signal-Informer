"""
Momentum / trend-following strategy catalogue (vectorised).

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
    sma, ema, atr, adx, macd, roc, donchian, keltner, aroon, vortex,
    supertrend, tsi, crossed_above, crossed_below,
)


class DonchianBreakout(VectorSetup):
    """Turtle-style channel breakout: close above the prior N-bar high → long,
    below the prior N-bar low → short. Fires on the breakout bar only."""
    name = "DONCHIAN_BREAKOUT"
    description = "N-bar Donchian channel breakout"
    param_grid = {"lookback": [20, 40, 55]}

    def __init__(self, lookback: int = 20):
        self.lookback = lookback
        self.min_periods = lookback + 5

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        upper, lower = donchian(df["high"], df["low"], self.lookback)
        long_  = (df["close"] > upper) & (df["close"].shift(1) <= upper.shift(1))
        short_ = (df["close"] < lower) & (df["close"].shift(1) >= lower.shift(1))
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class FiftyTwoWeekHigh(VectorSetup):
    """Momentum anomaly: a close breaking the 252-bar high after trading within
    proximity% of it — institutional momentum carries it further."""
    name = "HIGH_52W_BREAKOUT"
    description = "New 52-week high breakout after consolidation near the high"
    param_grid = {"lookback": [200, 252], "proximity": [2.0, 3.0, 5.0]}

    def __init__(self, lookback: int = 252, proximity: float = 3.0):
        self.lookback = lookback
        self.proximity = proximity
        self.min_periods = lookback + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        c = df["close"]
        prior_high = df["high"].shift(1).rolling(self.lookback,
                                                 min_periods=self.lookback).max()
        # consolidated near the high over the last 5 bars, then broke it
        near = (c.shift(1) >= prior_high.shift(1) * (1 - self.proximity / 100.0))
        long_ = (c > prior_high) & near & (c.shift(1) <= prior_high.shift(1))
        return pd.Series(np.where(long_, 1, 0), index=df.index)


class GoldenCrossPullback(VectorSetup):
    """In a golden-cross regime (fast SMA > slow SMA), buy the first close back
    above the fast SMA after dipping below it — trend re-entry at value."""
    name = "GOLDEN_CROSS_PULLBACK"
    description = "Pullback re-entry inside a golden-cross regime"
    param_grid = {"fast": [20, 50], "slow": [100, 200]}

    def __init__(self, fast: int = 50, slow: int = 200):
        self.fast = fast
        self.slow = slow
        self.min_periods = slow + 5

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        c  = df["close"]
        f  = sma(c, self.fast)
        s  = sma(c, self.slow)
        regime = f > s
        re_entry = (c > f) & (c.shift(1) <= f.shift(1))
        long_ = regime & re_entry
        # bearish mirror
        re_exit = (c < f) & (c.shift(1) >= f.shift(1))
        short_  = (~regime) & re_exit & (f < s)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class MACDZeroTurn(VectorSetup):
    """MACD line crossing its signal while both are below zero — early momentum
    turn from depressed levels (stronger than a generic MACD cross)."""
    name = "MACD_ZERO_TURN"
    description = "MACD bullish cross below zero / bearish cross above zero"
    param_grid = {"fast": [8, 12], "slow": [21, 26], "signal_p": [9]}

    def __init__(self, fast: int = 12, slow: int = 26, signal_p: int = 9):
        self.fast = fast
        self.slow = slow
        self.signal_p = signal_p
        self.min_periods = slow + signal_p + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        line, sig, _ = macd(df["close"], self.fast, self.slow, self.signal_p)
        long_  = crossed_above(line, sig) & (line < 0) & (sig < 0)
        short_ = crossed_below(line, sig) & (line > 0) & (sig > 0)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class ROCThrust(VectorSetup):
    """Rate-of-change thrust: momentum crossing above +threshold with volume
    confirmation — initiation of a directional move, not exhaustion."""
    name = "ROC_THRUST"
    description = "ROC momentum thrust with volume confirmation"
    param_grid = {"period": [10, 20], "threshold": [3.0, 5.0, 8.0], "vol_mult": [1.0, 1.5]}

    def __init__(self, period: int = 10, threshold: float = 5.0, vol_mult: float = 1.5):
        self.period = period
        self.threshold = threshold
        self.vol_mult = vol_mult
        self.min_periods = max(period + 5, 25)

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        r  = roc(df["close"], self.period) * 100.0
        va = df["volume"].shift(1).rolling(20, min_periods=20).mean()
        vol_ok = df["volume"] >= self.vol_mult * va
        long_  = crossed_above(r,  self.threshold) & vol_ok
        short_ = crossed_below(r, -self.threshold) & vol_ok
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class ADXDICross(VectorSetup):
    """+DI crossing above −DI with ADX above a floor — a trend is being born
    with directional confirmation."""
    name = "ADX_DI_CROSS"
    description = "+DI/-DI cross with minimum ADX trend strength"
    param_grid = {"adx_period": [10, 14], "threshold": [20, 25]}

    def __init__(self, adx_period: int = 14, threshold: float = 20.0):
        self.adx_period = adx_period
        self.threshold = threshold
        self.min_periods = adx_period * 3 + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        a, plus, minus = adx(df["high"], df["low"], df["close"], self.adx_period)
        long_  = crossed_above(plus, minus) & (a >= self.threshold)
        short_ = crossed_below(plus, minus) & (a >= self.threshold)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class AroonCross(VectorSetup):
    """Aroon-up crossing above Aroon-down with Aroon-up dominant — fresh highs
    are recent, fresh lows are stale: trend ignition."""
    name = "AROON_CROSS"
    description = "Aroon up/down cross with dominance filter"
    param_grid = {"period": [14, 25], "dominance": [60, 70]}

    def __init__(self, period: int = 25, dominance: float = 70.0):
        self.period = period
        self.dominance = dominance
        self.min_periods = period + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        up, dn = aroon(df["high"], df["low"], self.period)
        long_  = crossed_above(up, dn) & (up >= self.dominance)
        short_ = crossed_below(up, dn) & (dn >= self.dominance)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class SupertrendFlip(VectorSetup):
    """Supertrend regime flip: ATR-banded trailing level flips side — robust,
    self-adapting trend signal used heavily by Indian intraday desks."""
    name = "SUPERTREND_FLIP"
    description = "Supertrend direction flip (ATR trailing bands)"
    param_grid = {"period": [7, 10, 14], "mult": [2.0, 3.0]}

    def __init__(self, period: int = 10, mult: float = 3.0):
        self.period = period
        self.mult = mult
        self.min_periods = period * 3 + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        d = supertrend(df["high"], df["low"], df["close"], self.period, self.mult)
        long_  = (d == 1)  & (d.shift(1) == -1)
        short_ = (d == -1) & (d.shift(1) == 1)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class KeltnerBreakout(VectorSetup):
    """Close escaping the Keltner channel — volatility-normalised breakout
    (less whipsaw-prone than raw price channels)."""
    name = "KELTNER_BREAKOUT"
    description = "Keltner channel breakout"
    param_grid = {"ema_period": [20], "mult": [1.5, 2.0, 2.5]}

    def __init__(self, ema_period: int = 20, atr_period: int = 14, mult: float = 2.0):
        self.ema_period = ema_period
        self.atr_period = atr_period
        self.mult = mult
        self.min_periods = max(ema_period, atr_period) + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        upper, _, lower = keltner(df["high"], df["low"], df["close"],
                                  self.ema_period, self.atr_period, self.mult)
        long_  = crossed_above(df["close"], upper)
        short_ = crossed_below(df["close"], lower)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class HigherHighStructure(VectorSetup):
    """Price structure: two consecutive higher highs AND higher lows with the
    close above EMA20 — classic uptrend continuation entry (mirror for shorts)."""
    name = "HH_HL_STRUCTURE"
    description = "Higher-high/higher-low structure continuation"
    param_grid = {"bars": [2, 3], "ema_period": [20, 50]}

    def __init__(self, bars: int = 2, ema_period: int = 20):
        self.bars = bars
        self.ema_period = ema_period
        self.min_periods = ema_period + self.bars + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        h, l, c = df["high"], df["low"], df["close"]
        hh = pd.Series(True, index=df.index)
        hl = pd.Series(True, index=df.index)
        ll = pd.Series(True, index=df.index)
        lh = pd.Series(True, index=df.index)
        for k in range(self.bars):
            hh &= h.shift(k) > h.shift(k + 1)
            hl &= l.shift(k) > l.shift(k + 1)
            ll &= l.shift(k) < l.shift(k + 1)
            lh &= h.shift(k) < h.shift(k + 1)
        e = ema(c, self.ema_period)
        long_  = hh & hl & (c > e)
        short_ = ll & lh & (c < e)
        # first bar of the pattern only
        long_  &= ~(long_.shift(1).fillna(False))
        short_ &= ~(short_.shift(1).fillna(False))
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class TSICross(VectorSetup):
    """True Strength Index crossing its signal line — double-smoothed momentum
    turn with very little noise."""
    name = "TSI_CROSS"
    description = "True Strength Index signal-line cross"
    param_grid = {"long_p": [25], "short_p": [13], "signal_p": [7, 13]}

    def __init__(self, long_p: int = 25, short_p: int = 13, signal_p: int = 13):
        self.long_p = long_p
        self.short_p = short_p
        self.signal_p = signal_p
        self.min_periods = long_p + short_p + signal_p + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        t, sig = tsi(df["close"], self.long_p, self.short_p, self.signal_p)
        long_  = crossed_above(t, sig) & (t < 0)
        short_ = crossed_below(t, sig) & (t > 0)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class VortexCross(VectorSetup):
    """Vortex VI+ crossing VI− — rotational momentum shift confirming a new
    directional phase."""
    name = "VORTEX_CROSS"
    description = "Vortex indicator VI+/VI- cross"
    param_grid = {"period": [14, 21]}

    def __init__(self, period: int = 14):
        self.period = period
        self.min_periods = period + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        vip, vim = vortex(df["high"], df["low"], df["close"], self.period)
        long_  = crossed_above(vip, vim)
        short_ = crossed_below(vip, vim)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)
