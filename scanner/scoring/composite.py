"""Composite weighted sum of the nine signal strengths.

Returns a float in [0, 100]. Caller multiplies by the TOD multiplier to
obtain `final_score`.
"""

from __future__ import annotations

from collections.abc import Mapping

from .config import ScoringWeights
from .types import SignalResult


def composite_score(signals: Mapping[str, SignalResult], weights: ScoringWeights) -> float:
    weights_dict = weights.as_dict()
    raw = 0.0
    for name, weight in weights_dict.items():
        result = signals.get(name)
        if result is None:
            continue
        raw += weight * result.strength
    return raw * 100.0
