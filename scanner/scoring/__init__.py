"""Per-minute scoring engine.

Implements the 8-step scoring flow described in `PER_MINUTE_SCORING.md`:
gates → 9 signals → composite → TOD → bias → disqualifiers → tier.

`score_symbol(context)` is the public entry point. Everything else in this
package is pure helpers that take a `SignalContext` and return a number.
"""

from __future__ import annotations

from .config import (
    DEFAULT_TOD_MULTIPLIERS,
    DisqualifierConfig,
    GateThresholds,
    ScoringWeights,
    TierThresholds,
    TODMultipliers,
    TODWindow,
)
from .context import SignalContext
from .engine import score_symbol
from .types import Bar, Bias, ScoreResult, SignalResult, Tier

__all__ = [
    "Bar",
    "Bias",
    "DEFAULT_TOD_MULTIPLIERS",
    "DisqualifierConfig",
    "GateThresholds",
    "ScoreResult",
    "ScoringWeights",
    "SignalContext",
    "SignalResult",
    "TODMultipliers",
    "TODWindow",
    "Tier",
    "TierThresholds",
    "score_symbol",
]
