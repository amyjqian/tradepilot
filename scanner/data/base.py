"""MarketDataProvider abstract interface + OHLCV frame validator."""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


def validate_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Return df if it conforms to the OHLCV contract, else raise ValueError.

    Requirements:
      - columns == {open, high, low, close, volume} (lowercase)
      - DatetimeIndex, tz-aware, sorted ascending, unique
      - no NaN in OHLCV columns
    """
    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing OHLCV columns: {missing}; got {list(df.columns)}")

    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError(f"Index must be DatetimeIndex, got {type(df.index).__name__}")

    if df.index.tz is None:
        raise ValueError("DatetimeIndex must be tz-aware")

    if not df.index.is_monotonic_increasing:
        raise ValueError("DatetimeIndex must be sorted ascending")

    if not df.index.is_unique:
        raise ValueError("DatetimeIndex must be unique")

    if df[list(OHLCV_COLUMNS)].isna().any().any():
        raise ValueError("OHLCV columns must not contain NaN")

    return df


class MarketDataProvider(ABC):
    """Pluggable source of OHLCV bars."""

    @abstractmethod
    def get_bars(self, ticker: str, interval: str, lookback_days: int) -> pd.DataFrame:
        """Return bars for a single ticker."""

    @abstractmethod
    def get_bars_batch(
        self, tickers: list[str], interval: str, lookback_days: int
    ) -> dict[str, pd.DataFrame]:
        """Return bars for a batch of tickers. Missing tickers must be omitted, not raised."""
