"""
Data sanitiser and corporate-action detector.

Validation rules applied to every download before DB ingestion:
  1. No NaN in OHLCV columns
  2. All prices strictly positive
  3. Volume >= 0
  4. OHLC consistency: H >= max(O,C), L <= min(O,C)
  5. At least one row present

Anomaly detection (potential splits/bonus issues):
  - If the latest close deviates >50% from the last stored close,
    we flag it as a potential retroactive adjustment.
  - Caller decides whether to re-download full history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class ValidationResult:
    valid: bool
    symbol: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    adjustment_detected: bool = False


def validate(df: pd.DataFrame, symbol: str) -> ValidationResult:
    """
    Validate a downloaded OHLCV DataFrame.
    Returns a ValidationResult; result.valid=False means do NOT ingest.
    """
    result = ValidationResult(valid=True, symbol=symbol)

    if df is None or df.empty:
        result.valid = False
        result.errors.append("Empty DataFrame")
        return result

    required = ["open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        result.valid = False
        result.errors.append(f"Missing columns: {missing}")
        return result

    # 1. No NaN
    nan_counts = df[required].isnull().sum()
    for col, cnt in nan_counts.items():
        if cnt > 0:
            result.valid = False
            result.errors.append(f"Column '{col}' has {cnt} NaN values")

    # 2. Prices > 0
    for col in ["open", "high", "low", "close"]:
        non_positive = (df[col] <= 0).sum()
        if non_positive > 0:
            result.valid = False
            result.errors.append(f"Column '{col}' has {non_positive} non-positive values")

    # 3. Volume >= 0
    if (df["volume"] < 0).any():
        result.valid = False
        result.errors.append("Negative volume detected")

    # 4. OHLC consistency
    bad_high = (df["high"] < df[["open", "close"]].max(axis=1)).sum()
    bad_low  = (df["low"]  > df[["open", "close"]].min(axis=1)).sum()
    if bad_high > 0:
        result.warnings.append(f"{bad_high} rows where high < max(open,close) — rounding artefact?")
    if bad_low > 0:
        result.warnings.append(f"{bad_low} rows where low > min(open,close) — rounding artefact?")

    if result.warnings:
        for w in result.warnings:
            log.warning(f"sanitizer [{symbol}]: {w}")

    return result


def detect_adjustment(
    symbol: str,
    new_df: pd.DataFrame,
    last_stored_close: Optional[float],
    last_stored_date: Optional[str],
    threshold: float = 0.50,
) -> bool:
    """
    Return True if the earliest row in new_df shows a price deviation >threshold
    versus the last stored close — likely caused by a retroactive split/bonus adjustment.

    A deviation of >50% on a non-gap-up day strongly suggests a corporate action.
    """
    if last_stored_close is None or last_stored_date is None:
        return False

    # Find the row in new_df for last_stored_date
    new_df_dates = new_df.index.strftime("%Y-%m-%d").tolist()
    if last_stored_date not in new_df_dates:
        # Overlap date not in new download — can't compare
        return False

    new_close_for_stored_date = float(new_df.loc[new_df.index.strftime("%Y-%m-%d") == last_stored_date, "close"].iloc[0])

    deviation = abs(new_close_for_stored_date - last_stored_close) / last_stored_close
    if deviation > threshold:
        log.warning(
            f"sanitizer [{symbol}]: possible corporate action detected — "
            f"stored close={last_stored_close:.2f}, "
            f"new close for same date={new_close_for_stored_date:.2f}, "
            f"deviation={deviation:.1%}"
        )
        return True
    return False


def get_latest_row(df: pd.DataFrame) -> pd.Series | None:
    """Return the most recent row of the DataFrame, or None if empty."""
    if df is None or df.empty:
        return None
    return df.iloc[-1]
