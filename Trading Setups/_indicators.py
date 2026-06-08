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
