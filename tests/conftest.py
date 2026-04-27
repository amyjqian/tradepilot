"""Shared test fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def daily_index() -> pd.DatetimeIndex:
    return pd.date_range(end=datetime(2025, 1, 1, tzinfo=UTC), periods=120, freq="B", tz="UTC")


@pytest.fixture
def random_walk_close(daily_index: pd.DatetimeIndex) -> pd.Series:
    rng = np.random.default_rng(42)
    returns = rng.normal(loc=0.0005, scale=0.015, size=len(daily_index))
    prices = 100.0 * np.exp(np.cumsum(returns))
    return pd.Series(prices, index=daily_index)


@pytest.fixture
def constant_series(daily_index: pd.DatetimeIndex) -> pd.Series:
    return pd.Series(100.0, index=daily_index)


def make_ohlcv(
    close: pd.Series,
    volume: float | pd.Series = 1_000_000,
    intrabar: float = 0.005,
) -> pd.DataFrame:
    high = close * (1 + intrabar)
    low = close * (1 - intrabar)
    open_ = close.shift(1).fillna(close.iloc[0])
    if isinstance(volume, (int, float)):
        vol = pd.Series(float(volume), index=close.index)
    else:
        vol = volume
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol.astype(float)},
        index=close.index,
    )


@pytest.fixture
def bullish_ohlcv(daily_index: pd.DatetimeIndex) -> pd.DataFrame:
    """Steady uptrend with a volume spike on the final bar."""
    close = pd.Series(
        100.0 * np.exp(np.cumsum(np.full(len(daily_index), 0.008))),
        index=daily_index,
    )
    vol = np.full(len(daily_index), 1_000_000.0)
    vol[-3:] = 3_500_000.0  # strong rel-vol on recent bars
    return make_ohlcv(close, volume=pd.Series(vol, index=daily_index))


@pytest.fixture
def bearish_ohlcv(daily_index: pd.DatetimeIndex) -> pd.DataFrame:
    """Steady downtrend."""
    close = pd.Series(
        100.0 * np.exp(np.cumsum(np.full(len(daily_index), -0.008))),
        index=daily_index,
    )
    return make_ohlcv(close, volume=1_000_000.0)
