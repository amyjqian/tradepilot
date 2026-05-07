"""The nine scoring signals.

Each module exposes a single `compute(ctx) -> SignalResult`. Signals are
pure: they read from the context only, never raise on insufficient data
(they return `strength=0.0, raw=None` instead), and never see each other.
"""

from __future__ import annotations

from . import (
    breakout_proximity,
    clean_structure,
    momentum_atr,
    mtf_alignment,
    rsi_intraday,
    rvol_30m,
    rvol_cumulative,
    trend_stack_5m,
    vwap_distance_atr,
)

ALL_SIGNALS = (
    rvol_30m,
    rvol_cumulative,
    momentum_atr,
    vwap_distance_atr,
    trend_stack_5m,
    mtf_alignment,
    rsi_intraday,
    breakout_proximity,
    clean_structure,
)

__all__ = [
    "ALL_SIGNALS",
    "breakout_proximity",
    "clean_structure",
    "momentum_atr",
    "mtf_alignment",
    "rsi_intraday",
    "rvol_30m",
    "rvol_cumulative",
    "trend_stack_5m",
    "vwap_distance_atr",
]
