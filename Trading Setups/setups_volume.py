"""
Volume-driven strategy catalogue (vectorised).

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
    sma, ema, obv, ad_line, cmf, force_index, ease_of_movement,
    crossed_above, crossed_below,
)


class OBVDivergence(VectorSetup):
    """Price prints a new N-bar low but On-Balance-Volume holds above its own
    low — accumulation under the surface. Mirror for distribution at highs."""
    name = "OBV_DIVERGENCE"
    description = "OBV/price divergence at fresh extremes"
    param_grid = {"lookback": [15, 20, 30]}

    def __init__(self, lookback: int = 20):
        self.lookback = lookback
        self.min_periods = lookback + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        c  = df["close"]
        v  = obv(c, df["volume"])
        lb = self.lookback
        c_low  = c.shift(1).rolling(lb, min_periods=lb).min()
        v_low  = v.shift(1).rolling(lb, min_periods=lb).min()
        c_high = c.shift(1).rolling(lb, min_periods=lb).max()
        v_high = v.shift(1).rolling(lb, min_periods=lb).max()
        long_  = (c < c_low)  & (v > v_low)
        short_ = (c > c_high) & (v < v_high)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class PocketPivot(VectorSetup):
    """O'Neil/Morales pocket pivot: an up day whose volume exceeds the highest
    DOWN-day volume of the prior `lookback` days, above the 10-EMA — stealth
    institutional buying."""
    name = "POCKET_PIVOT"
    description = "Up-day volume beats all recent down-day volume"
    param_grid = {"lookback": [10, 15], "ema_period": [10, 20]}

    def __init__(self, lookback: int = 10, ema_period: int = 10):
        self.lookback = lookback
        self.ema_period = ema_period
        self.min_periods = max(lookback, ema_period) + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        c, v = df["close"], df["volume"]
        up_day = c > c.shift(1)
        down_vol = v.where(c < c.shift(1), 0.0)
        max_down = down_vol.shift(1).rolling(self.lookback,
                                             min_periods=self.lookback).max()
        e = ema(c, self.ema_period)
        long_ = up_day & (v > max_down) & (c > e)
        return pd.Series(np.where(long_, 1, 0), index=df.index)


class VolumeDryup(VectorSetup):
    """VDU at the lows: at/near an N-bar low, volume collapses below
    vol_ratio × its 20-bar average — supply is exhausted, the float is quiet.
    Buy the first sign of demand (close > prior close)."""
    name = "VOLUME_DRYUP"
    description = "Volume dry-up near the lows then first up close"
    param_grid = {"vol_ratio": [0.4, 0.5, 0.6], "extreme_lookback": [15, 20, 30]}

    def __init__(self, vol_ratio: float = 0.5, extreme_lookback: int = 20):
        self.vol_ratio = vol_ratio
        self.extreme_lookback = extreme_lookback
        self.min_periods = max(extreme_lookback, 20) + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        c, v, l = df["close"], df["volume"], df["low"]
        near_low = l.shift(1) <= l.shift(1).rolling(self.extreme_lookback,
                                                    min_periods=self.extreme_lookback).min() * 1.02
        avg_v  = v.rolling(20, min_periods=20).mean()
        dried  = v.shift(1) < self.vol_ratio * avg_v.shift(1)
        long_  = near_low & dried & (c > c.shift(1))
        return pd.Series(np.where(long_, 1, 0), index=df.index)


class CMFCross(VectorSetup):
    """Chaikin Money Flow crossing out of neutral: sustained closes near bar
    highs on volume (CMF > +thr) flag accumulation; the mirror flags
    distribution."""
    name = "CMF_CROSS"
    description = "Chaikin Money Flow threshold cross"
    param_grid = {"period": [20, 21], "threshold": [0.05, 0.10, 0.15]}

    def __init__(self, period: int = 20, threshold: float = 0.10):
        self.period = period
        self.threshold = threshold
        self.min_periods = period + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        m = cmf(df["high"], df["low"], df["close"], df["volume"], self.period)
        long_  = crossed_above(m,  self.threshold)
        short_ = crossed_below(m, -self.threshold)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class ForceIndexPullback(VectorSetup):
    """Elder: in an uptrend (close > trend SMA), a NEGATIVE 2-bar force index
    is a pullback to buy; in a downtrend a positive force index is a rally to
    short."""
    name = "FORCE_INDEX_PULLBACK"
    description = "Elder force-index pullback inside a trend"
    param_grid = {"ema_p": [2, 3], "trend_sma": [50, 100]}

    def __init__(self, ema_p: int = 2, trend_sma: int = 50):
        self.ema_p = ema_p
        self.trend_sma = trend_sma
        self.min_periods = trend_sma + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        fi    = force_index(df["close"], df["volume"], self.ema_p)
        trend = sma(df["close"], self.trend_sma)
        long_  = crossed_below(fi, 0.0) & (df["close"] > trend)
        short_ = crossed_above(fi, 0.0) & (df["close"] < trend)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class ADLineDivergence(VectorSetup):
    """Accumulation/Distribution line diverging from price at fresh extremes —
    same logic as OBV divergence but weighted by close position in range."""
    name = "AD_DIVERGENCE"
    description = "Accumulation/Distribution divergence at extremes"
    param_grid = {"lookback": [15, 20, 30]}

    def __init__(self, lookback: int = 20):
        self.lookback = lookback
        self.min_periods = lookback + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        c  = df["close"]
        ad = ad_line(df["high"], df["low"], c, df["volume"])
        lb = self.lookback
        c_low   = c.shift(1).rolling(lb, min_periods=lb).min()
        ad_low  = ad.shift(1).rolling(lb, min_periods=lb).min()
        c_high  = c.shift(1).rolling(lb, min_periods=lb).max()
        ad_high = ad.shift(1).rolling(lb, min_periods=lb).max()
        long_  = (c < c_low)  & (ad > ad_low)
        short_ = (c > c_high) & (ad < ad_high)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class HighVolumeThrust(VectorSetup):
    """Volume > vol_mult × average AND close above the prior bar's high —
    a thrust bar; institutions cannot hide size."""
    name = "HIGH_VOLUME_THRUST"
    description = "Heavy-volume thrust through the prior bar's extreme"
    param_grid = {"vol_mult": [2.0, 2.5, 3.0]}

    def __init__(self, vol_mult: float = 2.5, vol_period: int = 20):
        self.vol_mult = vol_mult
        self.vol_period = vol_period
        self.min_periods = vol_period + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        v  = df["volume"]
        va = v.shift(1).rolling(self.vol_period, min_periods=self.vol_period).mean()
        heavy = v > self.vol_mult * va
        long_  = heavy & (df["close"] > df["high"].shift(1))
        short_ = heavy & (df["close"] < df["low"].shift(1))
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class EaseOfMovementCross(VectorSetup):
    """Ease of Movement crossing zero: price advancing on light effort
    (volume) = path of least resistance."""
    name = "EOM_CROSS"
    description = "Ease-of-Movement zero-line cross"
    param_grid = {"period": [14, 20]}

    def __init__(self, period: int = 14):
        self.period = period
        self.min_periods = period + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        e = ease_of_movement(df["high"], df["low"], df["volume"], self.period)
        long_  = crossed_above(e, 0.0)
        short_ = crossed_below(e, 0.0)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)
