"""Core types for the scoring engine.

`Bar` is the lowest-level data primitive — a single OHLCV bar at any timeframe.
`SignalResult` is the output of one of the nine signals. `ScoreResult` is the
output of the full 8-step flow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, NamedTuple

Bias = Literal["long", "short", "neutral"]
Tier = Literal["A", "B", "C"]


class Bar(NamedTuple):
    """A single OHLCV bar.

    `ts_ms` is the bar's *close* timestamp in epoch milliseconds (UTC). All
    consumers in this package treat `bars[-1]` as "the most recently closed
    bar" and `bars[-1].close` as "now's price."
    """

    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class SignalResult:
    """One signal's output.

    `strength` is in [0, 1]. `raw` is the underlying value that was mapped
    onto the strength curve (e.g. an rvol ratio, an ATR-distance, an RSI).
    `raw` may be `None` when the signal could not be computed (insufficient
    bars), in which case `strength` is 0.0.
    """

    name: str
    strength: float
    raw: float | int | None = None


@dataclass(frozen=True)
class ScoreResult:
    """Output of `score_symbol`."""

    symbol: str
    timestamp: int
    base_score: float
    tod_mult: float
    final_score: float
    bias_15m: Bias
    flags: list[str] = field(default_factory=list)
    tier: Tier | None = None
    components: dict[str, SignalResult] = field(default_factory=dict)
