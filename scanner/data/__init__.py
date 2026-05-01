"""Data providers for the scanner."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from scanner.data.base import MarketDataProvider, validate_bars
from scanner.data.cache import BarCache, CachedProvider
from scanner.data.synthetic_provider import SyntheticProvider
from scanner.data.yfinance_provider import YFinanceProvider

ProviderName = Literal["yfinance", "synthetic", "ibkr", "polygon"]

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

    Supported names: 'synthetic', 'yfinance', 'ibkr' (alias 'ib'),
    'polygon' (alias 'massive'). Polygon and IB are imported lazily so
    their SDKs / API keys aren't required when not in use.
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
    elif name in ("polygon", "massive"):
        # Lazy import — only needs httpx + a POLYGON_API_KEY env var.
        from scanner.data.polygon_provider import PolygonProvider
        inner = PolygonProvider()
    else:
        raise ValueError(
            f"Unknown provider: {name!r} (expected 'synthetic', 'yfinance', "
            "'ibkr', or 'polygon'/'massive')"
        )

    if cache_path is not None:
        return CachedProvider(inner, BarCache(cache_path))
    return inner
