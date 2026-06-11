"""
Shared indicator helpers for Trading Setups.
This file starts with _ so setup_loader skips it.
"""
import numpy as np
import pandas as pd


def rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    result = result.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    result = result.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)
    return result


def adx(high: pd.Series, low: pd.Series, close: pd.Series,
        adx_period: int, di_period: int | None = None) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Returns (ADX, +DI, -DI) using Wilder's smoothing.
    di_period defaults to adx_period if not provided.
    """
    dp = di_period or adx_period

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    up   = high.diff()
    down = (-low).diff()

    plus_dm  = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)

    atr      = tr.ewm(com=adx_period - 1, min_periods=adx_period).mean()
    plus_di  = 100 * plus_dm.ewm(com=dp - 1, min_periods=dp).mean() / atr
    minus_di = 100 * minus_dm.ewm(com=dp - 1, min_periods=dp).mean() / atr

    dx  = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_series = dx.ewm(com=adx_period - 1, min_periods=adx_period).mean()

    return adx_series, plus_di, minus_di


def stochastic(high: pd.Series, low: pd.Series, close: pd.Series,
               k_period: int = 7, d_period: int = 10) -> tuple[pd.Series, pd.Series]:
    """Returns (%K, %D). %D is a simple MA of %K."""
    lo = low.rolling(k_period).min()
    hi = high.rolling(k_period).max()
    pct_k = 100 * (close - lo) / (hi - lo).replace(0, np.nan)
    pct_d = pct_k.rolling(d_period).mean()
    return pct_k, pct_d


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, min_periods=period).mean()


def historical_vol(close: pd.Series, period: int) -> pd.Series:
    """Annualized historical volatility (std dev of log returns × sqrt(252))."""
    log_ret = np.log(close / close.shift(1))
    return log_ret.rolling(period).std() * np.sqrt(252)


def is_inside_day(high: pd.Series, low: pd.Series) -> pd.Series:
    """True where today's high < yesterday's high AND today's low > yesterday's low."""
    return (high < high.shift(1)) & (low > low.shift(1))


def is_nr4(high: pd.Series, low: pd.Series, period: int = 4) -> pd.Series:
    """True where today's range is strictly narrower than any of the prior period-1 bars.

    Uses a shifted window (excludes the current bar) with strict < so that
    consecutive quiet bars at the same range level do NOT all fire — only
    the first bar that breaks below the prior minimum fires.
    """
    daily_range = high - low
    prior_min = daily_range.shift(1).rolling(period - 1, min_periods=period - 1).min()
    return daily_range < prior_min


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Wilder's ATR via EWM."""
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, min_periods=period).mean()


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (macd_line, signal_line, histogram)."""
    fast_ema  = close.ewm(span=fast,   min_periods=fast).mean()
    slow_ema  = close.ewm(span=slow,   min_periods=slow).mean()
    macd_line = fast_ema - slow_ema
    sig_line  = macd_line.ewm(span=signal, min_periods=signal).mean()
    return macd_line, sig_line, macd_line - sig_line


def bollinger_bands(
    close: pd.Series,
    period: int = 20,
    num_stddev: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (upper, middle, lower)."""
    middle = close.rolling(period, min_periods=period).mean()
    std    = close.rolling(period, min_periods=period).std()
    return middle + num_stddev * std, middle, middle - num_stddev * std


# ── Extended vectorised indicator library (used by the VectorSetup catalogue) ──

def roc(close: pd.Series, period: int) -> pd.Series:
    """Rate of change as a fraction (0.05 = +5%)."""
    return close / close.shift(period) - 1.0


def donchian(high: pd.Series, low: pd.Series, period: int) -> tuple[pd.Series, pd.Series]:
    """(upper, lower) channel of the PRIOR `period` bars (shifted — no look-ahead
    on the current bar's own extreme)."""
    upper = high.shift(1).rolling(period, min_periods=period).max()
    lower = low.shift(1).rolling(period, min_periods=period).min()
    return upper, lower


def keltner(high: pd.Series, low: pd.Series, close: pd.Series,
            ema_period: int = 20, atr_period: int = 14,
            mult: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Keltner channels: (upper, middle, lower)."""
    mid = ema(close, ema_period)
    rng = atr(high, low, close, atr_period)
    return mid + mult * rng, mid, mid - mult * rng


def cci(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20) -> pd.Series:
    tp  = (high + low + close) / 3.0
    ma  = tp.rolling(period, min_periods=period).mean()
    md  = (tp - ma).abs().rolling(period, min_periods=period).mean()
    return (tp - ma) / (0.015 * md.replace(0, np.nan))


def williams_r(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    hh = high.rolling(period, min_periods=period).max()
    ll = low.rolling(period, min_periods=period).min()
    return -100 * (hh - close) / (hh - ll).replace(0, np.nan)


def mfi(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series,
        period: int = 14) -> pd.Series:
    tp   = (high + low + close) / 3.0
    rmf  = tp * volume
    up   = rmf.where(tp > tp.shift(1), 0.0)
    down = rmf.where(tp < tp.shift(1), 0.0)
    pos  = up.rolling(period, min_periods=period).sum()
    neg  = down.rolling(period, min_periods=period).sum()
    return 100 - 100 / (1 + pos / neg.replace(0, np.nan))


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    sign = np.sign(close.diff()).fillna(0.0)
    return (sign * volume).cumsum()


def ad_line(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    rng = (high - low).replace(0, np.nan)
    clv = ((close - low) - (high - close)) / rng
    return (clv.fillna(0.0) * volume).cumsum()


def cmf(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series,
        period: int = 20) -> pd.Series:
    rng = (high - low).replace(0, np.nan)
    clv = ((close - low) - (high - close)) / rng
    mfv = clv.fillna(0.0) * volume
    return (mfv.rolling(period, min_periods=period).sum()
            / volume.rolling(period, min_periods=period).sum().replace(0, np.nan))


def force_index(close: pd.Series, volume: pd.Series, period: int = 2) -> pd.Series:
    fi = close.diff() * volume
    return fi.ewm(span=period, min_periods=period).mean()


def ease_of_movement(high: pd.Series, low: pd.Series, volume: pd.Series,
                     period: int = 14) -> pd.Series:
    mid_move = ((high + low) / 2).diff()
    box      = (volume / 1e6) / (high - low).replace(0, np.nan)
    return (mid_move / box).rolling(period, min_periods=period).mean()


def stoch_rsi(close: pd.Series, rsi_period: int = 14, stoch_period: int = 14,
              k_smooth: int = 3, d_smooth: int = 3) -> tuple[pd.Series, pd.Series]:
    """StochRSI %K and %D in 0-100."""
    r  = rsi(close, rsi_period)
    lo = r.rolling(stoch_period, min_periods=stoch_period).min()
    hi = r.rolling(stoch_period, min_periods=stoch_period).max()
    k  = 100 * (r - lo) / (hi - lo).replace(0, np.nan)
    k  = k.rolling(k_smooth, min_periods=k_smooth).mean()
    d  = k.rolling(d_smooth, min_periods=d_smooth).mean()
    return k, d


def tsi(close: pd.Series, long_p: int = 25, short_p: int = 13,
        signal_p: int = 13) -> tuple[pd.Series, pd.Series]:
    """True Strength Index and its signal line."""
    m   = close.diff()
    num = m.ewm(span=long_p, min_periods=long_p).mean().ewm(span=short_p, min_periods=short_p).mean()
    den = m.abs().ewm(span=long_p, min_periods=long_p).mean().ewm(span=short_p, min_periods=short_p).mean()
    t   = 100 * num / den.replace(0, np.nan)
    return t, t.ewm(span=signal_p, min_periods=signal_p).mean()


def trix(close: pd.Series, period: int = 15, signal_p: int = 9) -> tuple[pd.Series, pd.Series]:
    e1 = close.ewm(span=period, min_periods=period).mean()
    e2 = e1.ewm(span=period, min_periods=period).mean()
    e3 = e2.ewm(span=period, min_periods=period).mean()
    t  = 100 * e3.pct_change()
    return t, t.ewm(span=signal_p, min_periods=signal_p).mean()


def cmo(close: pd.Series, period: int = 14) -> pd.Series:
    """Chande Momentum Oscillator in [-100, 100]."""
    delta = close.diff()
    up    = delta.clip(lower=0).rolling(period, min_periods=period).sum()
    down  = (-delta).clip(lower=0).rolling(period, min_periods=period).sum()
    return 100 * (up - down) / (up + down).replace(0, np.nan)


def dpo(close: pd.Series, period: int = 20) -> pd.Series:
    """Detrended price oscillator (causal variant: close minus shifted SMA —
    avoids the classic centred look-ahead)."""
    return close - close.rolling(period, min_periods=period).mean().shift(period // 2 + 1)


def ppo(close: pd.Series, fast: int = 12, slow: int = 26,
        signal_p: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Percentage price oscillator: (ppo, signal, hist)."""
    f = close.ewm(span=fast, min_periods=fast).mean()
    s = close.ewm(span=slow, min_periods=slow).mean()
    p = 100 * (f - s) / s
    sig = p.ewm(span=signal_p, min_periods=signal_p).mean()
    return p, sig, p - sig


def aroon(high: pd.Series, low: pd.Series, period: int = 25) -> tuple[pd.Series, pd.Series]:
    """(aroon_up, aroon_down) in 0-100."""
    up = high.rolling(period + 1, min_periods=period + 1).apply(
        lambda x: 100 * float(np.argmax(x)) / period, raw=True)
    dn = low.rolling(period + 1, min_periods=period + 1).apply(
        lambda x: 100 * float(np.argmin(x)) / period, raw=True)
    return up, dn


def vortex(high: pd.Series, low: pd.Series, close: pd.Series,
           period: int = 14) -> tuple[pd.Series, pd.Series]:
    """(VI+, VI-)."""
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    vmp = (high - low.shift(1)).abs()
    vmm = (low - high.shift(1)).abs()
    trs = tr.rolling(period, min_periods=period).sum().replace(0, np.nan)
    return (vmp.rolling(period, min_periods=period).sum() / trs,
            vmm.rolling(period, min_periods=period).sum() / trs)


def supertrend(high: pd.Series, low: pd.Series, close: pd.Series,
               period: int = 10, mult: float = 3.0) -> pd.Series:
    """Supertrend direction: +1 bullish / -1 bearish per bar (0 until warm)."""
    a   = atr(high, low, close, period)
    mid = (high + low) / 2
    ub  = (mid + mult * a).to_numpy(dtype=float)
    lb  = (mid - mult * a).to_numpy(dtype=float)
    c   = close.to_numpy(dtype=float)
    n   = len(c)
    direction = np.zeros(n, dtype=np.int8)
    f_ub = np.full(n, np.nan)
    f_lb = np.full(n, np.nan)
    started = False
    for i in range(n):
        if np.isnan(ub[i]) or np.isnan(lb[i]):
            continue
        if not started:
            f_ub[i] = ub[i]; f_lb[i] = lb[i]
            direction[i] = 1 if c[i] > (ub[i] + lb[i]) / 2 else -1
            started = True
            continue
        f_ub[i] = ub[i] if (ub[i] < f_ub[i-1] or c[i-1] > f_ub[i-1]) else f_ub[i-1]
        f_lb[i] = lb[i] if (lb[i] > f_lb[i-1] or c[i-1] < f_lb[i-1]) else f_lb[i-1]
        if direction[i-1] == 1:
            direction[i] = -1 if c[i] < f_lb[i] else 1
        else:
            direction[i] = 1 if c[i] > f_ub[i] else -1
    return pd.Series(direction, index=close.index)


def ultimate_oscillator(high: pd.Series, low: pd.Series, close: pd.Series,
                        p1: int = 7, p2: int = 14, p3: int = 28) -> pd.Series:
    prev_close = close.shift(1)
    bp = close - pd.concat([low, prev_close], axis=1).min(axis=1)
    tr = (pd.concat([high, prev_close], axis=1).max(axis=1)
          - pd.concat([low, prev_close], axis=1).min(axis=1)).replace(0, np.nan)
    a1 = bp.rolling(p1, min_periods=p1).sum() / tr.rolling(p1, min_periods=p1).sum()
    a2 = bp.rolling(p2, min_periods=p2).sum() / tr.rolling(p2, min_periods=p2).sum()
    a3 = bp.rolling(p3, min_periods=p3).sum() / tr.rolling(p3, min_periods=p3).sum()
    return 100 * (4 * a1 + 2 * a2 + a3) / 7


def ibs(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Internal bar strength: (close - low) / (high - low) in [0, 1]."""
    return (close - low) / (high - low).replace(0, np.nan)


def zscore(close: pd.Series, period: int = 20) -> pd.Series:
    m = close.rolling(period, min_periods=period).mean()
    s = close.rolling(period, min_periods=period).std()
    return (close - m) / s.replace(0, np.nan)


def crossed_above(a: pd.Series, b) -> pd.Series:
    """True on bars where a crosses from below to at/above b (scalar or Series)."""
    if not isinstance(b, pd.Series):
        b = pd.Series(b, index=a.index)
    return (a >= b) & (a.shift(1) < b.shift(1))


def crossed_below(a: pd.Series, b) -> pd.Series:
    """True on bars where a crosses from above to at/below b (scalar or Series)."""
    if not isinstance(b, pd.Series):
        b = pd.Series(b, index=a.index)
    return (a <= b) & (a.shift(1) > b.shift(1))
