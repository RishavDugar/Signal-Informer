"""
Price-pattern / candlestick strategy catalogue (vectorised).

All patterns are quantified (no subjective chart reading) and most are
anchored to an n-bar extreme so they fire at location, not in the middle of
a range — pattern + location is what carries the edge.

Direction convention: +1 long, -1 short/sell, 0 nothing.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.vector_setup import VectorSetup

sys.path.insert(0, str(Path(__file__).parent))
from _indicators import sma


class EngulfingAtExtreme(VectorSetup):
    """Bullish engulfing at an N-bar low (body engulfs prior body after a
    decline) / bearish engulfing at an N-bar high."""
    name = "ENGULFING_EXTREME"
    description = "Engulfing bar at an n-bar extreme"
    param_grid = {"lookback": [10, 20, 30]}

    def __init__(self, lookback: int = 20):
        self.lookback = lookback
        self.min_periods = lookback + 5

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
        bull = (c > o) & (c.shift(1) < o.shift(1)) & (c >= o.shift(1)) & (o <= c.shift(1))
        bear = (c < o) & (c.shift(1) > o.shift(1)) & (c <= o.shift(1)) & (o >= c.shift(1))
        at_low  = l <= l.shift(1).rolling(self.lookback, min_periods=self.lookback).min()
        at_high = h >= h.shift(1).rolling(self.lookback, min_periods=self.lookback).max()
        long_  = bull & at_low
        short_ = bear & at_high
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class MorningStar(VectorSetup):
    """Three-bar reversal: strong down bar, small-body pause bar, then a strong
    up bar closing above the midpoint of bar 1. Evening-star mirror for shorts."""
    name = "MORNING_STAR"
    description = "Morning/evening star 3-bar reversal"
    param_grid = {"body_ratio": [0.3, 0.5]}

    def __init__(self, body_ratio: float = 0.5):
        # pause bar's body must be < body_ratio × bar-1 body
        self.body_ratio = body_ratio
        self.min_periods = 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        o, c = df["open"], df["close"]
        body   = (c - o)
        b1down = body.shift(2) < 0
        b1up   = body.shift(2) > 0
        small  = body.shift(1).abs() < self.body_ratio * body.shift(2).abs()
        mid1   = (o.shift(2) + c.shift(2)) / 2
        long_  = b1down & small & (c > o) & (c > mid1)
        short_ = b1up   & small & (c < o) & (c < mid1)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class ThreeWhiteSoldiers(VectorSetup):
    """Three consecutive strong up closes (each body > min_body × range, each
    close higher) after a decline → trend ignition. Three black crows mirror."""
    name = "THREE_SOLDIERS"
    description = "Three white soldiers / three black crows after a swing"
    param_grid = {"min_body": [0.5, 0.6], "decline_lookback": [10, 15]}

    def __init__(self, min_body: float = 0.6, decline_lookback: int = 10):
        self.min_body = min_body
        self.decline_lookback = decline_lookback
        self.min_periods = decline_lookback + 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
        rng    = (h - l).replace(0, np.nan)
        solid_up   = ((c - o) / rng > self.min_body)
        solid_down = ((o - c) / rng > self.min_body)
        rising  = (c > c.shift(1))
        falling = (c < c.shift(1))
        three_up   = (solid_up & rising
                      & solid_up.shift(1).fillna(False) & rising.shift(1).fillna(False)
                      & solid_up.shift(2).fillna(False) & rising.shift(2).fillna(False))
        three_down = (solid_down & falling
                      & solid_down.shift(1).fillna(False) & falling.shift(1).fillna(False)
                      & solid_down.shift(2).fillna(False) & falling.shift(2).fillna(False))
        # location: pattern starts from a local low (for soldiers) / high (crows)
        was_low  = c.shift(3) <= c.shift(3).rolling(self.decline_lookback,
                                                    min_periods=self.decline_lookback).min() * 1.02
        was_high = c.shift(3) >= c.shift(3).rolling(self.decline_lookback,
                                                    min_periods=self.decline_lookback).max() * 0.98
        long_  = three_up & was_low
        short_ = three_down & was_high
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class PiercingLine(VectorSetup):
    """Open gaps below the prior low but closes above the midpoint of the prior
    (down) body — failed breakdown, buyers in control. Dark-cloud mirror."""
    name = "PIERCING_LINE"
    description = "Piercing line / dark cloud cover"
    param_grid = {}

    def __init__(self):
        self.min_periods = 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
        mid_prior  = (o.shift(1) + c.shift(1)) / 2
        prior_down = c.shift(1) < o.shift(1)
        prior_up   = c.shift(1) > o.shift(1)
        long_  = prior_down & (o < l.shift(1)) & (c > mid_prior) & (c < o.shift(1))
        short_ = prior_up   & (o > h.shift(1)) & (c < mid_prior) & (c > o.shift(1))
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class KeyReversal(VectorSetup):
    """Key reversal bar: trades to a new N-bar low intrabar but closes above
    the prior close — a one-bar failed breakdown. Mirror at highs."""
    name = "KEY_REVERSAL"
    description = "New n-bar extreme intrabar, close back through prior close"
    param_grid = {"lookback": [10, 20, 30]}

    def __init__(self, lookback: int = 20):
        self.lookback = lookback
        self.min_periods = lookback + 5

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        h, l, c = df["high"], df["low"], df["close"]
        lb = self.lookback
        new_low  = l < l.shift(1).rolling(lb, min_periods=lb).min()
        new_high = h > h.shift(1).rolling(lb, min_periods=lb).max()
        long_  = new_low  & (c > c.shift(1))
        short_ = new_high & (c < c.shift(1))
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class DojiAtExtreme(VectorSetup):
    """A doji (body < body_pct of range) printed at an N-bar extreme = stalemate
    exactly where one side should dominate — fade the prior move on the next
    bar's confirmation close."""
    name = "DOJI_EXTREME"
    description = "Doji at an n-bar extreme plus confirmation close"
    param_grid = {"body_pct": [0.10, 0.15], "lookback": [10, 20]}

    def __init__(self, body_pct: float = 0.10, lookback: int = 20):
        self.body_pct = body_pct
        self.lookback = lookback
        self.min_periods = lookback + 5

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
        rng  = (h - l).replace(0, np.nan)
        doji = ((c - o).abs() / rng) < self.body_pct
        at_low  = l <= l.shift(1).rolling(self.lookback, min_periods=self.lookback).min()
        at_high = h >= h.shift(1).rolling(self.lookback, min_periods=self.lookback).max()
        long_  = doji.shift(1).fillna(False) & at_low.shift(1).fillna(False)  & (c > h.shift(1))
        short_ = doji.shift(1).fillna(False) & at_high.shift(1).fillna(False) & (c < l.shift(1))
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class MarubozuContinuation(VectorSetup):
    """Marubozu: a full-body bar (body > body_pct of range) with above-average
    volume — one side controlled the entire session; continuation favoured."""
    name = "MARUBOZU_CONT"
    description = "Full-body conviction bar with volume — continuation"
    param_grid = {"body_pct": [0.85, 0.90, 0.95], "vol_mult": [1.2, 1.5]}

    def __init__(self, body_pct: float = 0.90, vol_mult: float = 1.5):
        self.body_pct = body_pct
        self.vol_mult = vol_mult
        self.min_periods = 25

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]
        rng = (h - l).replace(0, np.nan)
        va  = v.shift(1).rolling(20, min_periods=20).mean()
        full_up   = ((c - o) / rng >= self.body_pct) & (v > self.vol_mult * va)
        full_down = ((o - c) / rng >= self.body_pct) & (v > self.vol_mult * va)
        return pd.Series(np.where(full_up, 1, np.where(full_down, -1, 0)), index=df.index)


class OopsReversal(VectorSetup):
    """Larry Williams OOPS: open gaps below the prior LOW, then price recovers
    to close above the prior low — overnight panic absorbed. Mirror for upside
    gaps that fail back under the prior high."""
    name = "OOPS_REVERSAL"
    description = "Gap beyond the prior extreme recovered intraday"
    param_grid = {}

    def __init__(self):
        self.min_periods = 10

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
        long_  = (o < l.shift(1)) & (c > l.shift(1))
        short_ = (o > h.shift(1)) & (c < h.shift(1))
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)
