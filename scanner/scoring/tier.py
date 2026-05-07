"""Tier assignment — A / B / C / None (spec step 8).

A and B require `bias == "long"`. C admits any bias (the score alone is
high enough to be worth a look). The thresholds are inclusive (`>=`).
"""

from __future__ import annotations

from .config import TierThresholds
from .types import Bias, Tier


def assign_tier(final_score: float, bias: Bias, thresholds: TierThresholds) -> Tier | None:
    if final_score >= thresholds.a and bias == "long":
        return "A"
    if final_score >= thresholds.b and bias == "long":
        return "B"
    if final_score >= thresholds.c:
        return "C"
    return None
