"""Data providers for the scanner."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from scanner.data.base import MarketDataProvider, validate_bars
from scanner.data.cache import BarCache, CachedProvider
from scanner.data.synthetic_provider import SyntheticProvider
from scanner.data.yfinance_provider import YFinanceProvider

ProviderName = Literal["yfinance", "synthetic", "ibkr"]

__all__ = [
    "MarketDataProvider",
    "validate_bars",
    "BarCache",
    "CachedProvider",
    "SyntheticProvider",
    "YFinanceProvider",
    "get_provider",
]


def get_provider(name: str, cache_path: str | Path | None = None) -> MarketDataProvider:
    """Factory for providers, optionally wrapped in a CachedProvider.

    Supported names: 'synthetic', 'yfinance', 'ibkr' (alias 'ib').
    The IB provider is imported lazily so ib_async is only a soft dependency.
    """
    inner: MarketDataProvider
    if name == "synthetic":
        inner = SyntheticProvider()
    elif name == "yfinance":
        inner = YFinanceProvider()
    elif name in ("ibkr", "ib"):
        # Lazy import — ib_async is optional.
        from scanner.data.ib_provider import IBProvider
        inner = IBProvider()
    else:
        raise ValueError(
            f"Unknown provider: {name!r} (expected 'synthetic', 'yfinance', or 'ibkr')"
        )

    if cache_path is not None:
        return CachedProvider(inner, BarCache(cache_path))
    return inner
