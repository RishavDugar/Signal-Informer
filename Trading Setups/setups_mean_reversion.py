"""
Mean-reversion strategy catalogue (vectorised).

All setups subclass VectorSetup: they compute signals for every bar in one
pass (fast enough for daily AND intraday data) and derive the classic
signal() interface automatically.

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
    rsi, sma, ema, atr, mfi, cci, williams_r, stochastic, ibs, zscore,
    bollinger_bands, crossed_above, crossed_below,
)


class RSI2Extreme(VectorSetup):
    """Connors RSI(2): deep short-term oversold in an uptrend mean-reverts.
    Long when RSI(2) < lo with close above the long-term trend SMA; short when
    RSI(2) > hi below the trend SMA. Cross semantics — fires on entry bar only."""
    name = "RSI2_EXTREME"
    description = "Connors RSI(2) deep oversold/overbought with trend filter"
    param_grid = {"period": [2, 3, 4], "lo": [5, 10, 15], "hi": [85, 90, 95],
                  "trend_sma": [100, 200]}

    def __init__(self, period: int = 2, lo: float = 10.0, hi: float = 90.0,
                 trend_sma: int = 200):
        self.period = period
        self.lo = lo
        self.hi = hi
        self.trend_sma = trend_sma
        self.min_periods = max(trend_sma + 1, 30)

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        r     = rsi(df["close"], self.period)
        trend = sma(df["close"], self.trend_sma)
        long_  = crossed_below(r, self.lo) & (df["close"] > trend)
        short_ = crossed_above(r, self.hi) & (df["close"] < trend)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class DoubleSevens(VectorSetup):
    """Connors Double 7s: in an uptrend, a 7-day closing low is a buy;
    in a downtrend, a 7-day closing high is a sell."""
    name = "DOUBLE_SEVENS"
    description = "N-day closing extreme against the prevailing trend"
    param_grid = {"lookback": [5, 7, 10], "trend_sma": [100, 200]}

    def __init__(self, lookback: int = 7, trend_sma: int = 200):
        self.lookback = lookback
        self.trend_sma = trend_sma
        self.min_periods = max(trend_sma + 1, 30)

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        c     = df["close"]
        trend = sma(c, self.trend_sma)
        lo_n  = c.shift(1).rolling(self.lookback, min_periods=self.lookback).min()
        hi_n  = c.shift(1).rolling(self.lookback, min_periods=self.lookback).max()
        long_  = (c < lo_n) & (c > trend)
        short_ = (c > hi_n) & (c < trend)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class IBSReversal(VectorSetup):
    """Internal Bar Strength: closes pinned to the bar's low (IBS < lo_thr)
    revert up next session; closes pinned to the high revert down."""
    name = "IBS_REVERSAL"
    description = "Internal bar strength extreme reversal"
    param_grid = {"lo_thr": [0.05, 0.10, 0.15], "hi_thr": [0.85, 0.90, 0.95],
                  "trend_sma": [0, 200]}

    def __init__(self, lo_thr: float = 0.10, hi_thr: float = 0.90, trend_sma: int = 0):
        self.lo_thr = lo_thr
        self.hi_thr = hi_thr
        self.trend_sma = trend_sma   # 0 = no trend filter
        self.min_periods = max(trend_sma + 1, 20)

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        x = ibs(df["open"], df["high"], df["low"], df["close"])
        long_  = x < self.lo_thr
        short_ = x > self.hi_thr
        if self.trend_sma:
            trend  = sma(df["close"], self.trend_sma)
            long_  &= df["close"] > trend
            short_ &= df["close"] < trend
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class ZScoreReversion(VectorSetup):
    """Buy when price is z_entry std-devs below its n-bar mean (statistical
    cheapness), sell-short when equally stretched above. Fires on the cross."""
    name = "ZSCORE_REVERSION"
    description = "Z-score of close vs rolling mean — fade the extremes"
    param_grid = {"period": [15, 20, 30], "z_entry": [1.5, 2.0, 2.5]}

    def __init__(self, period: int = 20, z_entry: float = 2.0):
        self.period = period
        self.z_entry = z_entry
        self.min_periods = max(period + 2, 25)

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        z = zscore(df["close"], self.period)
        long_  = crossed_below(z, -self.z_entry)
        short_ = crossed_above(z,  self.z_entry)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class BollingerTag(VectorSetup):
    """Close beyond a Bollinger band snaps back toward the middle band.
    Long on a close below the lower band, short on a close above the upper."""
    name = "BOLLINGER_TAG"
    description = "Close outside Bollinger band — mean reversion to the middle"
    param_grid = {"period": [15, 20, 25], "num_stddev": [2.0, 2.5, 3.0]}

    def __init__(self, period: int = 20, num_stddev: float = 2.0):
        self.period = period
        self.num_stddev = num_stddev
        self.min_periods = period + 2

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        upper, _, lower = bollinger_bands(df["close"], self.period, self.num_stddev)
        long_  = crossed_below(df["close"], lower)
        short_ = crossed_above(df["close"], upper)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class GapDownReversal(VectorSetup):
    """A gap down of >= gap_pct in a stock trading above its trend SMA tends to
    fill — panic supply into an intact uptrend. Long at the close of the gap bar."""
    name = "GAP_DOWN_REVERSAL"
    description = "Gap-down in an uptrend — buy the fear"
    param_grid = {"gap_pct": [1.0, 2.0, 3.0], "trend_sma": [50, 100, 200]}

    def __init__(self, gap_pct: float = 2.0, trend_sma: int = 50):
        self.gap_pct = gap_pct
        self.trend_sma = trend_sma
        self.min_periods = max(trend_sma + 1, 30)

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        gap   = df["open"] / df["close"].shift(1) - 1.0
        trend = sma(df["close"], self.trend_sma)
        long_ = (gap <= -self.gap_pct / 100.0) & (df["close"].shift(1) > trend.shift(1))
        return pd.Series(np.where(long_, 1, 0), index=df.index)


class CapitulationReversal(VectorSetup):
    """Multi-day waterfall (drop_pct over days) PLUS a volume spike — classic
    capitulation. The forced sellers are done; buy the exhaustion."""
    name = "CAPITULATION_REVERSAL"
    description = "Sharp multi-day decline with climactic volume — exhaustion buy"
    param_grid = {"drop_pct": [5.0, 8.0, 12.0], "days": [3, 5],
                  "vol_mult": [1.5, 2.0, 3.0]}

    def __init__(self, drop_pct: float = 8.0, days: int = 5, vol_mult: float = 2.0):
        self.drop_pct = drop_pct
        self.days = days
        self.vol_mult = vol_mult
        self.min_periods = 30

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        ret_k   = df["close"] / df["close"].shift(self.days) - 1.0
        vol_avg = df["volume"].shift(1).rolling(20, min_periods=20).mean()
        long_   = (ret_k <= -self.drop_pct / 100.0) & (df["volume"] > self.vol_mult * vol_avg)
        return pd.Series(np.where(long_, 1, 0), index=df.index)


class RSIDivergence(VectorSetup):
    """Price makes a lower low but RSI makes a higher low inside `lookback`
    bars — sellers are losing force. Inverse for shorts."""
    name = "RSI_DIVERGENCE"
    description = "Price/RSI divergence at new lows or highs"
    param_grid = {"period": [9, 14], "lookback": [10, 15, 20]}

    def __init__(self, period: int = 14, lookback: int = 15):
        self.period = period
        self.lookback = lookback
        self.min_periods = max(period + lookback + 5, 40)

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        c  = df["close"]
        r  = rsi(c, self.period)
        lb = self.lookback
        prior_low_c  = c.shift(1).rolling(lb, min_periods=lb).min()
        prior_low_r  = r.shift(1).rolling(lb, min_periods=lb).min()
        prior_high_c = c.shift(1).rolling(lb, min_periods=lb).max()
        prior_high_r = r.shift(1).rolling(lb, min_periods=lb).max()
        long_  = (c < prior_low_c)  & (r > prior_low_r)
        short_ = (c > prior_high_c) & (r < prior_high_r)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class StochOversoldHook(VectorSetup):
    """Stochastic %K deep in the oversold zone turning back up (hook) — the
    first sign sellers are exhausted. Mirror image for shorts."""
    name = "STOCH_HOOK"
    description = "Stochastic %K hook out of an extreme zone"
    param_grid = {"k_period": [5, 9, 14], "lo": [15, 20, 25], "hi": [75, 80, 85]}

    def __init__(self, k_period: int = 9, d_period: int = 3,
                 lo: float = 20.0, hi: float = 80.0):
        self.k_period = k_period
        self.d_period = d_period
        self.lo = lo
        self.hi = hi
        self.min_periods = k_period + d_period + 5

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        k, _ = stochastic(df["high"], df["low"], df["close"],
                          self.k_period, self.d_period)
        long_  = (k.shift(1) < self.lo) & (k > k.shift(1)) & (k.shift(2) >= k.shift(1))
        short_ = (k.shift(1) > self.hi) & (k < k.shift(1)) & (k.shift(2) <= k.shift(1))
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class WickRejection(VectorSetup):
    """Hammer-style rejection: at an n-day low, a bar with a long lower shadow
    (shadow_ratio × body) closing in the top of its range = demand absorption.
    Shooting-star mirror at n-day highs."""
    name = "WICK_REJECTION"
    description = "Long-shadow rejection bar at an n-day extreme"
    param_grid = {"shadow_ratio": [1.5, 2.0, 3.0], "extreme_lookback": [10, 20]}

    def __init__(self, shadow_ratio: float = 2.0, extreme_lookback: int = 20,
                 range_pos: float = 0.60):
        self.shadow_ratio = shadow_ratio
        self.extreme_lookback = extreme_lookback
        self.range_pos = range_pos
        self.min_periods = extreme_lookback + 5

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
        body  = (c - o).abs()
        rng   = (h - l).replace(0, np.nan)
        lower_shadow = pd.concat([o, c], axis=1).min(axis=1) - l
        upper_shadow = h - pd.concat([o, c], axis=1).max(axis=1)
        pos   = (c - l) / rng
        at_low  = l <= l.shift(1).rolling(self.extreme_lookback,
                                          min_periods=self.extreme_lookback).min()
        at_high = h >= h.shift(1).rolling(self.extreme_lookback,
                                          min_periods=self.extreme_lookback).max()
        long_  = at_low  & (lower_shadow >= self.shadow_ratio * body) & (pos >= self.range_pos)
        short_ = at_high & (upper_shadow >= self.shadow_ratio * body) & (pos <= 1 - self.range_pos)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class StreakFade(VectorSetup):
    """N consecutive down closes → long (a streak that long is statistically
    stretched); N consecutive up closes → short."""
    name = "STREAK_FADE"
    description = "Fade a run of N consecutive same-direction closes"
    param_grid = {"streak": [3, 4, 5, 6]}

    def __init__(self, streak: int = 4):
        self.streak = streak
        self.min_periods = streak + 5

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        chg  = np.sign(df["close"].diff())
        down = (chg < 0).rolling(self.streak, min_periods=self.streak).sum() == self.streak
        up   = (chg > 0).rolling(self.streak, min_periods=self.streak).sum() == self.streak
        # only the FIRST bar the streak completes
        long_  = down & ~down.shift(1).fillna(False)
        short_ = up   & ~up.shift(1).fillna(False)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class MFIExtreme(VectorSetup):
    """Money Flow Index (volume-weighted RSI) extremes: <lo = panic outflow
    (buy), >hi = euphoric inflow (sell). Fires on the cross out."""
    name = "MFI_EXTREME"
    description = "Money Flow Index extreme with cross semantics"
    param_grid = {"period": [10, 14], "lo": [15, 20, 25], "hi": [75, 80, 85]}

    def __init__(self, period: int = 14, lo: float = 20.0, hi: float = 80.0):
        self.period = period
        self.lo = lo
        self.hi = hi
        self.min_periods = period + 5

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        m = mfi(df["high"], df["low"], df["close"], df["volume"], self.period)
        long_  = crossed_below(m, self.lo)
        short_ = crossed_above(m, self.hi)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class WilliamsRExtreme(VectorSetup):
    """Williams %R below -90 marks washed-out closes near n-day lows; above
    -10 marks frothy closes at the highs. Fade both."""
    name = "WILLIAMS_R_EXTREME"
    description = "Williams %R deep extreme fade"
    param_grid = {"period": [10, 14, 20], "lo": [-95, -90, -85], "hi": [-15, -10, -5]}

    def __init__(self, period: int = 14, lo: float = -90.0, hi: float = -10.0):
        self.period = period
        self.lo = lo
        self.hi = hi
        self.min_periods = period + 5

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        w = williams_r(df["high"], df["low"], df["close"], self.period)
        long_  = crossed_below(w, self.lo)
        short_ = crossed_above(w, self.hi)
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)


class CCIReversal(VectorSetup):
    """CCI beyond ±threshold then turning back — momentum exhaustion fade."""
    name = "CCI_REVERSAL"
    description = "CCI extreme with a turn back toward the mean"
    param_grid = {"period": [14, 20], "threshold": [100, 150, 200]}

    def __init__(self, period: int = 20, threshold: float = 150.0):
        self.period = period
        self.threshold = threshold
        self.min_periods = period + 5

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        x = cci(df["high"], df["low"], df["close"], self.period)
        long_  = (x.shift(1) < -self.threshold) & (x > x.shift(1))
        short_ = (x.shift(1) >  self.threshold) & (x < x.shift(1))
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)
