from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass
class SignalResult:
    signal: bool
    symbol: str
    setup_name: str
    date: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "signal": self.signal,
            "symbol": self.symbol,
            "setup_name": self.setup_name,
            "date": self.date,
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


class BaseSetup(ABC):
    """
    Abstract base for all trading setups.

    Subclasses must set class-level `name` and `description` and implement `signal()`.
    `min_periods` declares how many rows of OHLCV the setup needs at minimum.

    `sl_pct` is a universal meta-parameter injected post-construction by the backtester.
    When set (e.g. sl_pct=2.0), the stoploss is placed exactly 2% below/above the entry
    price, overriding the setup's native `get_stoploss()` logic.  None = use native SL.
    """

    name: str = ""
    description: str = ""
    min_periods: int = 1
    sl_pct: float | None = None   # overridden by backtester/hypersearch when tuning SL distance

    # ── Public interface ──────────────────────────────────────────────────────

    @abstractmethod
    def signal(self, df: pd.DataFrame, symbol: str) -> SignalResult:
        """
        Compute signal for the most recent bar in `df`.

        `df` has lowercase columns: open, high, low, close, volume.
        Index is a DatetimeIndex sorted ascending.
        """

    # ── Helpers available to subclasses ──────────────────────────────────────

    def validate_data(self, df: pd.DataFrame) -> tuple[bool, str]:
        """Returns (is_valid, error_message)."""
        required = {"open", "high", "low", "close", "volume"}
        missing = required - set(df.columns)
        if missing:
            return False, f"Missing columns: {sorted(missing)}"
        if len(df) < self.min_periods:
            return False, (
                f"{self.name} needs {self.min_periods} periods, got {len(df)}"
            )
        if df[["open", "high", "low", "close"]].isnull().any().any():
            return False, "NaN values present in price columns"
        return True, ""

    def _error_result(self, symbol: str, error: str) -> SignalResult:
        return SignalResult(
            signal=False,
            symbol=symbol,
            setup_name=self.name,
            date="",
            metadata={"error": error},
        )

    def get_stoploss(self, result: "SignalResult", df: pd.DataFrame) -> float | None:
        """Return the stoploss price for this signal, or None.

        Default: signal-bar low for buy/neutral; signal-bar high for sell/short.
        Override in subclasses where the strategy specifies a different rule.
        """
        meta = result.metadata
        st = str(meta.get("signal_type", "") or meta.get("condition", "")).lower()
        if st in ("sell", "short", "overbought"):
            return float(df["high"].iloc[-1])
        return float(df["low"].iloc[-1])

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
