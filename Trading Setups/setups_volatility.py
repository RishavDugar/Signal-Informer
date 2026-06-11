"""
Volatility-compression / expansion strategy catalogue (vectorised).

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
    sma, ema, atr, bollinger_bands, keltner, macd,
    crossed_above, crossed_below,
)


class NR7Breakout(VectorSetup):
    """Crabel NR7: the narrowest range of 7 bars compresses energy; trade the
    next bar's break of the NR7 bar's extreme. Signal fires on the breakout
    bar (close beyond the NR7 high → long; below NR7 low → short)."""
    name = "NR7_BREAKOUT"
    description = "Narrowest-range-7 compression then range break"
    param_grid = {"period": [4, 7, 10]}

    def __init__(self, period: int = 7):
        self.period = period
        self.min_periods = period + 5

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        rng       = df["high"] - df["low"]
        prior_min = rng.shift(1).rolling(self.period - 1,
                                         min_periods=self.period - 1).min()
        nr        = (rng < prior_min).shift(1).fillna(False)   # yesterday was NR
        long_  = nr & (df["close"] > df["high"].shift(1))
        short_ = nr & (df["close"] < df["low"].shift(1))
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class TTMSqueeze(VectorSetup):
    """TTM Squeeze: Bollinger bands inside Keltner channels = energy stored.
    When the squeeze RELEASES, go with the momentum direction."""
    name = "TTM_SQUEEZE"
    description = "Bollinger-inside-Keltner squeeze release with momentum"
    param_grid = {"bb_period": [20], "bb_std": [2.0], "kc_mult": [1.5, 2.0]}

    def __init__(self, bb_period: int = 20, bb_std: float = 2.0, kc_mult: float = 1.5):
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.kc_mult = kc_mult
        self.min_periods = bb_period + 15

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        bb_u, bb_m, bb_l = bollinger_bands(df["close"], self.bb_period, self.bb_std)
        kc_u, _, kc_l    = keltner(df["high"], df["low"], df["close"],
                                   self.bb_period, self.bb_period, self.kc_mult)
        squeezed = (bb_u < kc_u) & (bb_l > kc_l)
        released = (~squeezed) & squeezed.shift(1).fillna(False)
        mom = df["close"] - df["close"].rolling(self.bb_period,
                                                min_periods=self.bb_period).mean()
        long_  = released & (mom > 0)
        short_ = released & (mom < 0)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class ATRCompressionBreak(VectorSetup):
    """Short-term ATR collapsed vs long-term ATR (quiet tape), then a range
    break — volatility cycles from contraction to expansion."""
    name = "ATR_COMPRESSION_BREAK"
    description = "ATR ratio compression followed by a breakout"
    param_grid = {"short_p": [5, 10], "long_p": [50], "ratio": [0.5, 0.6, 0.7]}

    def __init__(self, short_p: int = 10, long_p: int = 50, ratio: float = 0.6):
        self.short_p = short_p
        self.long_p = long_p
        self.ratio = ratio
        self.min_periods = long_p + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        h, l, c = df["high"], df["low"], df["close"]
        compressed = (atr(h, l, c, self.short_p)
                      < self.ratio * atr(h, l, c, self.long_p)).shift(1).fillna(False)
        long_  = compressed & (c > h.shift(1))
        short_ = compressed & (c < l.shift(1))
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class VolatilityBreakout(VectorSetup):
    """Larry Williams volatility breakout: close exceeding prior close by
    k × ATR is a statistically abnormal range day — momentum follows."""
    name = "VOLATILITY_BREAKOUT"
    description = "Close beyond prior close ± k×ATR"
    param_grid = {"atr_period": [10, 14], "k": [1.0, 1.5, 2.0]}

    def __init__(self, atr_period: int = 14, k: float = 1.5):
        self.atr_period = atr_period
        self.k = k
        self.min_periods = atr_period + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        a = atr(df["high"], df["low"], df["close"], self.atr_period).shift(1)
        c = df["close"]
        long_  = c > c.shift(1) + self.k * a
        short_ = c < c.shift(1) - self.k * a
        # fire only on the first expansion bar, not every bar of a runaway move
        long_  &= ~(long_.shift(1).fillna(False))
        short_ &= ~(short_.shift(1).fillna(False))
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class InsideBarBreakout(VectorSetup):
    """Inside bar = one-bar consolidation. Trade the break of the mother bar's
    extreme on the following bar."""
    name = "INSIDE_BAR_BREAKOUT"
    description = "Mother-bar breakout after an inside bar"
    param_grid = {}

    def __init__(self):
        self.min_periods = 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        h, l, c = df["high"], df["low"], df["close"]
        inside = ((h < h.shift(1)) & (l > l.shift(1))).shift(1).fillna(False)
        mother_high = h.shift(2)
        mother_low  = l.shift(2)
        long_  = inside & (c > mother_high)
        short_ = inside & (c < mother_low)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class RangeExpansionClose(VectorSetup):
    """A range > mult × ATR with the close pinned in the directional extreme of
    the bar = institutional initiative; continuation is favoured."""
    name = "RANGE_EXPANSION"
    description = "Wide-range bar closing at its extreme — continuation"
    param_grid = {"mult": [1.5, 2.0, 2.5], "close_pos": [0.75, 0.85]}

    def __init__(self, mult: float = 2.0, close_pos: float = 0.80, atr_period: int = 14):
        self.mult = mult
        self.close_pos = close_pos
        self.atr_period = atr_period
        self.min_periods = atr_period + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        h, l, c = df["high"], df["low"], df["close"]
        rng = (h - l)
        a   = atr(h, l, c, self.atr_period).shift(1)
        pos = (c - l) / rng.replace(0, np.nan)
        wide = rng > self.mult * a
        long_  = wide & (pos >= self.close_pos)
        short_ = wide & (pos <= 1 - self.close_pos)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class GapAndGo(VectorSetup):
    """Gap up beyond gap_pct that HOLDS (close > open) = initiative buying that
    absorbed all profit-taking; momentum tends to continue. Mirror for downside."""
    name = "GAP_AND_GO"
    description = "Held gap in the gap direction — continuation"
    param_grid = {"gap_pct": [1.0, 2.0, 3.0]}

    def __init__(self, gap_pct: float = 2.0):
        self.gap_pct = gap_pct
        self.min_periods = 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        gap = df["open"] / df["close"].shift(1) - 1.0
        long_  = (gap >= self.gap_pct / 100.0)  & (df["close"] > df["open"])
        short_ = (gap <= -self.gap_pct / 100.0) & (df["close"] < df["open"])
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class BBWidthSqueeze(VectorSetup):
    """Bollinger bandwidth at an N-bar minimum (historic compression), then a
    close outside a band picks the expansion direction."""
    name = "BB_WIDTH_SQUEEZE"
    description = "Bollinger bandwidth N-bar low then band escape"
    param_grid = {"period": [20], "lookback": [50, 100]}

    def __init__(self, period: int = 20, lookback: int = 100):
        self.period = period
        self.lookback = lookback
        self.min_periods = period + lookback + 5

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        upper, mid, lower = bollinger_bands(df["close"], self.period, 2.0)
        width = (upper - lower) / mid.replace(0, np.nan)
        was_tight = (width.shift(1)
                     <= width.shift(1).rolling(self.lookback,
                                               min_periods=self.lookback).min())
        long_  = was_tight & (df["close"] > upper)
        short_ = was_tight & (df["close"] < lower)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)
