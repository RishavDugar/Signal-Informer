"""
EMA Trend Pullback

Institutional pullback-and-reclaim setup: price dips below a fast EMA during
an uptrend (close > slow EMA), then crosses back above the fast EMA.

Buy signal:
  1. Uptrend confirmed: current close > slow EMA.
  2. The price was below the fast EMA for at least 1 bar and at most
     `lookback` bars immediately before the current bar.
     (Ensures a genuine brief pullback, not a prolonged breakdown.)
  3. Current bar closes back above the fast EMA (reclaim).

Sell signal: mirror image — downtrend (close < slow EMA), price briefly
  crossed above fast EMA, now crosses back below.

Entry: close of the reclaim bar.
Stop: low of the dip (buy) / high of the bounce (sell).

Metadata:
  signal_type  : "buy" or "sell"
  fast_ema_val : current fast EMA value
  slow_ema_val : current slow EMA value
  dip_bars     : consecutive bars the price was below/above the fast EMA
  close        : current close
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from core.base_setup import BaseSetup, SignalResult

sys.path.insert(0, str(Path(__file__).parent))
from _indicators import ema as compute_ema


class EmaTrendPullbackSetup(BaseSetup):
    name        = "EMA_TREND_PULLBACK"
    description = "Brief dip below fast EMA while above slow EMA then reclaim — pullback entry"
    min_periods = 220

    def __init__(
        self,
        fast_ema: int = 50,
        slow_ema: int = 200,
        lookback: int = 5,
    ):
        self.fast_ema    = fast_ema
        self.slow_ema    = slow_ema
        self.lookback    = lookback
        self.min_periods = slow_ema + lookback + 5

    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        valid, err = self.validate_data(df)
        if not valid:
            return self._error_result(symbol, err)

        date = df.index[-1].strftime("%Y-%m-%d")

        fast_s = compute_ema(df["close"], self.fast_ema)
        slow_s = compute_ema(df["close"], self.slow_ema)

        cur_fast = float(fast_s.iloc[-1])
        cur_slow = float(slow_s.iloc[-1])
        cur_close = float(df["close"].iloc[-1])
        prev_close = float(df["close"].iloc[-2])
        prev_fast  = float(fast_s.iloc[-2])

        if np.isnan(cur_fast) or np.isnan(cur_slow):
            return self._error_result(symbol, "EMA is NaN")

        def _dip_streak(below: bool) -> int:
            """Count consecutive bars before current that were below (or above) fast EMA."""
            streak = 0
            for i in range(2, min(self.lookback + 3, len(df))):
                c = float(df["close"].iloc[-i])
                f = float(fast_s.iloc[-i])
                if np.isnan(f):
                    break
                if below and c <= f:
                    streak += 1
                elif not below and c >= f:
                    streak += 1
                else:
                    break
            return streak

        # ── Buy: reclaim fast EMA in an uptrend ──────────────────────────────
        in_uptrend   = cur_close > cur_slow
        just_reclaim = cur_close > cur_fast and prev_close <= prev_fast
        if in_uptrend and just_reclaim:
            dip_bars = _dip_streak(below=True)
            if 1 <= dip_bars <= self.lookback:
                return SignalResult(
                    signal=True, symbol=symbol, setup_name=self.name, date=date,
                    metadata={
                        "signal_type" : "buy",
                        "fast_ema_val": round(cur_fast, 2),
                        "slow_ema_val": round(cur_slow, 2),
                        "dip_bars"    : dip_bars,
                        "close"       : round(cur_close, 2),
                    },
                )

        # ── Sell: drop below fast EMA in a downtrend ─────────────────────────
        in_downtrend  = cur_close < cur_slow
        just_crossed  = cur_close < cur_fast and prev_close >= prev_fast
        if in_downtrend and just_crossed:
            bounce_bars = _dip_streak(below=False)
            if 1 <= bounce_bars <= self.lookback:
                return SignalResult(
                    signal=True, symbol=symbol, setup_name=self.name, date=date,
                    metadata={
                        "signal_type" : "sell",
                        "fast_ema_val": round(cur_fast, 2),
                        "slow_ema_val": round(cur_slow, 2),
                        "dip_bars"    : bounce_bars,
                        "close"       : round(cur_close, 2),
                    },
                )

        return SignalResult(
            signal=False, symbol=symbol, setup_name=self.name, date=date,
            metadata={
                "fast_ema_val": round(cur_fast, 2),
                "slow_ema_val": round(cur_slow, 2),
                "close"       : round(cur_close, 2),
            },
        )

    @staticmethod
    def _run_length(flags: pd.Series) -> pd.Series:
        """Consecutive-True run length ending at each bar."""
        f = flags.fillna(False).astype(int)
        grp = (f == 0).cumsum()
        return f.groupby(grp).cumsum()

    def vector_signals(self, df: pd.DataFrame) -> pd.Series:
        """Vectorised equivalent: brief dip below fast EMA (1..lookback bars)
        inside a slow-EMA trend, then a reclaim cross."""
        import numpy as np
        c      = df["close"]
        fast_s = compute_ema(c, self.fast_ema)
        slow_s = compute_ema(c, self.slow_ema)

        below = c <= fast_s
        above = c >= fast_s
        dip_len    = self._run_length(below).shift(1)
        bounce_len = self._run_length(above).shift(1)

        long_ = ((c > slow_s) & (c > fast_s) & below.shift(1).fillna(False)
                 & (dip_len >= 1) & (dip_len <= self.lookback))
        short_ = ((c < slow_s) & (c < fast_s) & above.shift(1).fillna(False)
                  & (bounce_len >= 1) & (bounce_len <= self.lookback))
        return pd.Series(np.where(long_, 1, np.where(short_, -1, 0)), index=df.index)
