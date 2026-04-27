"""Deterministic synthetic OHLCV provider for offline testing.

Each ticker is assigned a regime (bullish/bearish/choppy) by a stable hash so the
same ticker always produces the same bars, regardless of the run.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Literal

import numpy as np
import pandas as pd

from scanner.data.base import MarketDataProvider, validate_bars

Regime = Literal["bullish", "bearish", "choppy"]

_REGIME_PARAMS: dict[Regime, dict[str, float]] = {
    "bullish": {"drift": 0.008, "vol": 0.015, "up_vol_boost": 2.0},
    "bearish": {"drift": -0.006, "vol": 0.018, "up_vol_boost": 1.0},
    "choppy": {"drift": 0.0005, "vol": 0.020, "up_vol_boost": 1.1},
}

_INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1d": 60 * 24}


def _stable_hash(ticker: str) -> int:
    digest = hashlib.sha256(ticker.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _regime_for(ticker: str) -> Regime:
    h = _stable_hash(ticker)
    bucket = h % 3
    return ("bullish", "bearish", "choppy")[bucket]


def _base_price_for(ticker: str) -> float:
    h = _stable_hash(ticker)
    # Spread base prices in [20, 300) so the universe filter has something to do.
    return 20.0 + (h % 28000) / 100.0


def _base_volume_for(ticker: str) -> float:
    h = _stable_hash(ticker)
    return 1_000_000 + (h % 4_500_000)


def _bar_count(interval: str, lookback_days: int) -> int:
    minutes = _INTERVAL_MINUTES.get(interval, _INTERVAL_MINUTES["1d"])
    if interval == "1d":
        return max(1, lookback_days)
    # Assume 6.5 trading hours/day for intraday.
    per_day = max(1, int(390 / minutes))
    return max(1, lookback_days * per_day)


def _timestamps(interval: str, n: int, end: datetime) -> pd.DatetimeIndex:
    if interval == "1d":
        idx = pd.date_range(end=end.date(), periods=n, freq="B", tz="UTC")
        return idx
    minutes = _INTERVAL_MINUTES[interval]
    idx = pd.date_range(end=end, periods=n, freq=f"{minutes}min", tz="UTC")
    return idx


class SyntheticProvider(MarketDataProvider):
    """GBM OHLCV generator keyed deterministically by ticker."""

    def __init__(self, end: datetime | None = None) -> None:
        # `end` lets tests pin a reference time; default is 2025-01-01 UTC so results
        # are fully deterministic across runs.
        self._end = end or datetime(2025, 1, 1, tzinfo=UTC)

    def _generate(self, ticker: str, interval: str, lookback_days: int) -> pd.DataFrame:
        regime = _regime_for(ticker)
        params = _REGIME_PARAMS[regime]
        n = _bar_count(interval, lookback_days)

        # Seed from ticker hash so each ticker is reproducible and independent.
        seed = _stable_hash(ticker) & 0xFFFFFFFF
        rng = np.random.default_rng(seed)

        drift = float(params["drift"])
        vol = float(params["vol"])
        up_vol_boost = float(params["up_vol_boost"])

        # Scale drift/vol to the bar interval (daily params by default).
        if interval != "1d":
            minutes = _INTERVAL_MINUTES[interval]
            scale = minutes / (60 * 24)
            drift = drift * scale
            vol = vol * (scale**0.5)

        log_returns = rng.normal(loc=drift, scale=vol, size=n)
        price_path = _base_price_for(ticker) * np.exp(np.cumsum(log_returns))

        # Build OHLC from a noisy intra-bar range.
        intrabar = np.abs(rng.normal(0, vol / 2, size=n)) + 1e-4
        opens = np.empty(n)
        opens[0] = _base_price_for(ticker)
        opens[1:] = price_path[:-1]
        closes = price_path
        highs = np.maximum(opens, closes) * (1 + intrabar)
        lows = np.minimum(opens, closes) * (1 - intrabar)

        base_vol = _base_volume_for(ticker)
        up_day = closes > opens
        vol_noise = rng.lognormal(mean=0.0, sigma=0.3, size=n)
        volumes = base_vol * vol_noise * np.where(up_day, up_vol_boost, 1.0)
        volumes = np.maximum(volumes, 1000.0).astype(np.int64)

        idx = _timestamps(interval, n, self._end)
        df = pd.DataFrame(
            {
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volumes,
            },
            index=idx,
        )
        return validate_bars(df)

    def get_bars(self, ticker: str, interval: str, lookback_days: int) -> pd.DataFrame:
        return self._generate(ticker, interval, lookback_days)

    def get_bars_batch(
        self, tickers: list[str], interval: str, lookback_days: int
    ) -> dict[str, pd.DataFrame]:
        return {t: self._generate(t, interval, lookback_days) for t in tickers}
